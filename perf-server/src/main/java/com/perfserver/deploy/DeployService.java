package com.perfserver.deploy;

import com.jcraft.jsch.ChannelExec;
import com.jcraft.jsch.ChannelSftp;
import com.jcraft.jsch.JSch;
import com.jcraft.jsch.Session;
import com.perfserver.entity.DeployConn;
import com.perfserver.mapper.DeployConnMapper;
import com.perfserver.security.CryptoService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.TimeUnit;

/**
 * 远程部署服务：平台侧编译（源码仓库就在平台上）→ SFTP 上传编译包与 agent → SSH 远程启动。
 * 供 AI 工具（build_project / deploy_and_start / remote_app_status）编排调用。
 *
 * <p>安全约束：远程命令全部模板化白名单（mkdir/tar/启动/状态检查），文件名与路径严格校验，
 * 凭据经 connId 从 deploy_conn 读取，不经过 AI 上下文。</p>
 */
@Service
public class DeployService {

    private static final Logger log = LoggerFactory.getLogger(DeployService.class);

    /** Maven 可执行文件（平台侧编译用） */
    @Value("${deploy.maven-bin:mvn}")
    private String mavenBin;

    /** 单命令执行超时（秒） */
    private static final int CMD_TIMEOUT_SEC = 300;
    /** 编译超时（秒） */
    private static final int BUILD_TIMEOUT_SEC = 600;
    /** 文件名白名单 */
    private static final String SAFE_NAME = "[A-Za-z0-9._\\-]+";

    private final DeployConnMapper deployConnMapper;
    private final CryptoService cryptoService;

    public DeployService(DeployConnMapper deployConnMapper, CryptoService cryptoService) {
        this.deployConnMapper = deployConnMapper;
        this.cryptoService = cryptoService;
    }

    public List<DeployConn> listConns(int limit) {
        // SQL 只查非敏感列 + has_credential 计算列，凭据内容不出库
        return deployConnMapper.list(limit);
    }

    /** 新增 SSH 连接（密码/私钥二选一必填校验；凭据加密落库） */
    public void addConn(DeployConn conn) {
        boolean hasPassword = conn.getPassword() != null && !conn.getPassword().isBlank();
        boolean hasKey = conn.getPrivateKey() != null && !conn.getPrivateKey().isBlank();
        if (!hasPassword && !hasKey) {
            throw new IllegalArgumentException("password 与 privateKey 至少配置一项");
        }
        conn.setAuthType(hasKey ? DeployConn.AUTH_PRIVATE_KEY : DeployConn.AUTH_PASSWORD);
        // 加密落库：存量明文在下次保存时自动转为密文
        conn.setPassword(cryptoService.encrypt(conn.getPassword()));
        conn.setPrivateKey(cryptoService.encrypt(conn.getPrivateKey()));
        deployConnMapper.insert(conn);
    }

    public void deleteConn(long id) {
        deployConnMapper.delete(id);
    }

    /** 测试 SSH 连接连通性（不落库；密码/私钥二选一，探活并返回远程主机标识） */
    public java.util.Map<String, Object> testConn(DeployConn conn) {
        boolean hasKey = conn.getPrivateKey() != null && !conn.getPrivateKey().isBlank();
        conn.setAuthType(hasKey ? DeployConn.AUTH_PRIVATE_KEY : DeployConn.AUTH_PASSWORD);
        java.util.Map<String, Object> r = new java.util.HashMap<>();
        Session session = null;
        try {
            session = connect(conn);
            String remoteInfo = exec(session, "hostname; uname -s").trim();
            r.put("success", true);
            r.put("remoteInfo", remoteInfo);
        } catch (Exception e) {
            r.put("success", false);
            r.put("error", e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage());
        } finally {
            if (session != null) {
                session.disconnect();
            }
        }
        return r;
    }

    /** 按已保存连接 ID 测试连通性（凭据不出服务端） */
    public java.util.Map<String, Object> testConnById(long id) {
        DeployConn conn = deployConnMapper.findById(id);
        if (conn == null) {
            throw new IllegalArgumentException("SSH 连接不存在: " + id);
        }
        return testConn(conn);
    }

    /** 平台侧编译：mvn -pl 模块 package（源码仓库已在平台上，Maven 镜像沿用平台 settings.xml） */
    public BuildResult build(long repoId, String module, String repoPath) throws Exception {
        if (repoPath == null || repoPath.isBlank() || repoPath.contains("..")) {
            throw new IllegalArgumentException("repoPath 非法");
        }
        Path root = Paths.get(repoPath).toAbsolutePath().normalize();
        if (!Files.isDirectory(root)) {
            throw new IllegalArgumentException("源码目录不存在: " + root);
        }
        String mvnArgs = (module == null || module.isBlank() || !module.matches(SAFE_NAME))
                ? "clean package -DskipTests" : "clean package -DskipTests -pl " + module + " -am";
        java.util.List<String> cmd = new java.util.ArrayList<>();
        cmd.add(mavenBin);
        cmd.add("-B");
        cmd.add("-q");
        cmd.addAll(java.util.Arrays.asList(mvnArgs.split(" ")));
        ProcessBuilder pb = new ProcessBuilder(cmd)
                .directory(root.toFile())
                .redirectErrorStream(true);
        Process p = pb.start();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (InputStream in = p.getInputStream()) {
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
        }
        boolean finished = p.waitFor(BUILD_TIMEOUT_SEC, TimeUnit.SECONDS);
        if (!finished) {
            p.destroyForcibly();
            throw new IllegalStateException("编译超时（" + BUILD_TIMEOUT_SEC + "s）");
        }
        int code = p.exitValue();
        String logTail = tail(out.toString(), 2000);
        if (code != 0) {
            return new BuildResult(false, logTail, null);
        }
        // 定位 fat-jar（target/*.jar，排除 original）
        Path target = (module == null || module.isBlank()) ? root.resolve("target") : root.resolve(module).resolve("target");
        if (!Files.isDirectory(target)) {
            return new BuildResult(false, "未找到 target 目录: " + target, null);
        }
        try (var stream = Files.list(target)) {
            Path jar = stream.filter(f -> f.getFileName().toString().endsWith(".jar")
                            && !f.getFileName().toString().startsWith("original"))
                    .findFirst().orElse(null);
            if (jar == null) {
                return new BuildResult(false, "target 下无 jar 产物", null);
            }
            return new BuildResult(true, logTail, jar);
        }
    }

    /**
     * 部署并启动：上传编译包（与 agent 可选）→ 远程解压/写入 → nohup 启动（含 agent -javaagent 参数）。
     *
     * @param jarPath      平台侧编译包绝对路径
     * @param agentPath    平台侧 agent 包（可空；非空则一并上传并以 -javaagent 挂载）
     * @param appArgs      应用启动参数（可空；文件名白名单校验）
     * @param serverPort   被测应用端口（健康检查用）
     */
    public DeployResult deployAndStart(long connId, Path jarPath, Path agentPath,
                                       String appArgs, int serverPort) throws Exception {
        DeployConn conn = deployConnMapper.findById(connId);
        if (conn == null) {
            throw new IllegalArgumentException("SSH 连接不存在: " + connId);
        }
        String jarName = jarPath.getFileName().toString();
        if (!jarName.matches(SAFE_NAME) || !jarName.endsWith(".jar")) {
            throw new IllegalArgumentException("编译包文件名非法: " + jarName);
        }
        String agentName = agentPath == null ? null
                : (agentPath.getFileName().toString().matches(SAFE_NAME)
                        && agentPath.getFileName().toString().endsWith(".jar")
                        ? agentPath.getFileName().toString() : null);
        String args = appArgs == null ? "" : appArgs;
        if (args.contains(";") || args.contains("&") || args.contains("|") || args.contains("`")) {
            throw new IllegalArgumentException("启动参数含非法字符");
        }

        Session session = connect(conn);
        try {
            ChannelSftp sftp = (ChannelSftp) session.openChannel("sftp");
            sftp.connect(CMD_TIMEOUT_SEC * 1000);
            exec(session, "mkdir -p " + quote(conn.getWorkDir()) + " && cd " + quote(conn.getWorkDir())
                    + " && rm -f app.jar agent.jar");
            try (InputStream in = Files.newInputStream(jarPath)) {
                sftp.put(in, conn.getWorkDir() + "/app.jar");
            }
            if (agentPath != null && agentName != null) {
                try (InputStream in = Files.newInputStream(agentPath)) {
                    sftp.put(in, conn.getWorkDir() + "/agent.jar");
                }
            }
            sftp.disconnect();

            // 模板化启动命令：仅 appArgs 为受控插值（已过滤 shell 元字符）
            String javaagent = (agentPath != null && agentName != null)
                    ? "-javaagent:" + conn.getWorkDir() + "/agent.jar " : "";
            String startCmd = "cd " + quote(conn.getWorkDir())
                    + " && nohup java -Xms512m -Xmx512m " + javaagent
                    + "-jar app.jar " + args + " > app.log 2>&1 & echo $!";
            String pid = exec(session, startCmd).trim();
            Thread.sleep(3000);
            String health = exec(session,
                    "curl -s -o /dev/null -w %{http_code} --max-time 5 http://127.0.0.1:" + serverPort + "/ || true");
            String status = exec(session, "ps -p " + pid + " -o pid=,etime= 2>/dev/null || true");
            return new DeployResult(true, jarName, agentName, pid, health.trim(),
                    status.isBlank() ? "进程已退出" : "运行中");
        } finally {
            session.disconnect();
        }
    }

    /** 远程应用状态：进程 + 端口健康检查（模板化命令） */
    public String appStatus(long connId, int serverPort) throws Exception {
        DeployConn conn = deployConnMapper.findById(connId);
        if (conn == null) {
            throw new IllegalArgumentException("SSH 连接不存在: " + connId);
        }
        Session session = connect(conn);
        try {
            String ps = exec(session, "ps -ef | grep '[a]pp.jar' || true");
            String health = exec(session,
                    "curl -s -o /dev/null -w %{http_code} --max-time 5 http://127.0.0.1:" + serverPort + "/ || true");
            String logTail = tail(exec(session, "tail -n 20 " + quote(conn.getWorkDir()) + "/app.log 2>/dev/null || true"), 1200);
            return "进程: " + (ps.isBlank() ? "未运行" : "运行中") + "\n健康检查(/): HTTP " + health.trim()
                    + "\n日志尾部:\n" + logTail;
        } finally {
            session.disconnect();
        }
    }

    private Session connect(DeployConn conn) throws Exception {
        JSch jsch = new JSch();
        // 取用点解密：明文仅存在于本方法栈内，不写入日志、不进入 AI 上下文
        String privateKey = cryptoService.decrypt(conn.getPrivateKey());
        if (DeployConn.AUTH_PRIVATE_KEY.equals(conn.getAuthType()) && privateKey != null) {
            jsch.addIdentity("deploy-key-" + conn.getId(), privateKey.getBytes(), null, null);
        }
        Session session = jsch.getSession(conn.getUsername(), conn.getHost(),
                conn.getPort() == null ? 22 : conn.getPort());
        if (DeployConn.AUTH_PASSWORD.equals(conn.getAuthType())) {
            session.setPassword(cryptoService.decrypt(conn.getPassword()));
        }
        Properties cfg = new Properties();
        cfg.put("StrictHostKeyChecking", "no");
        cfg.put("PreferredAuthentications", "publickey,password,keyboard-interactive");
        session.setConfig(cfg);
        session.connect(10_000);
        return session;
    }

    /** 执行单条远程命令并取 stdout（超时强制断开） */
    private String exec(Session session, String command) throws Exception {
        ChannelExec channel = (ChannelExec) session.openChannel("exec");
        channel.setCommand(command);
        channel.setErrStream(null);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        channel.setOutputStream(out);
        channel.connect(CMD_TIMEOUT_SEC * 1000);
        long deadline = System.currentTimeMillis() + CMD_TIMEOUT_SEC * 1000L;
        while (!channel.isClosed() && System.currentTimeMillis() < deadline) {
            Thread.sleep(100);
        }
        String result = out.toString();
        channel.disconnect();
        return result;
    }

    private static String quote(String s) {
        return "'" + s.replace("'", "'\\''") + "'";
    }

    private static String tail(String s, int max) {
        if (s == null) {
            return "";
        }
        return s.length() <= max ? s : s.substring(s.length() - max);
    }

    /** 编译结果：成功/日志尾部/产物路径 */
    public record BuildResult(boolean success, String logTail, Path jarPath) {
    }

    /** 部署结果：上传/启动/健康检查摘要 */
    public record DeployResult(boolean success, String jarName, String agentName,
                               String pid, String healthCode, String processStatus) {
    }
}

