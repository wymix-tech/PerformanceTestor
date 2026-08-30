# 环境重建指南（工具链安装）

> 适用场景：Linux 容器重建后 `/opt/java`、`/opt/maven` 丢失，需重新安装后端构建工具链。
> Node 22 为环境内置，无需安装；前端依赖重装一条命令（见文末）。
> 首次记录：2026-08-30（M0 执行时安装）；镜像源均选国内可用源。

## 1. JDK 17（Adoptium / Temurin）

```bash
# 清华镜像下载（注意：镜像目录按版本组织，先列目录再选文件名，文件名随小版本更新）
curl -sSL "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/17/jdk/x64/linux/" \
  | grep -o 'OpenJDK17U-jdk_x64_linux_hotspot[^"]*\.tar\.gz' | sort -uV | tail -1

# 安装（以实际文件名为准，本记录为 17.0.20.1_1）
cd /tmp && curl -sSL --connect-timeout 15 -o jdk17.tar.gz \
  "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/17/jdk/x64/linux/OpenJDK17U-jdk_x64_linux_hotspot_17.0.20.1_1.tar.gz"
mkdir -p /opt/java && tar -xzf jdk17.tar.gz -C /opt/java
/opt/java/jdk-17.0.20.1+1/bin/java -version   # 验证 openjdk version "17.0.20.1"
```

**失败回退**：阿里云镜像 `https://mirrors.aliyun.com/adoptium/17/jdk/x64/linux/`（目录索引可能 404，清华源更稳）；archive.apache.org 不提供 JDK。

## 2. Maven 3.9.6

```bash
# 注意：tuna 的 apache/maven 目录只有最新版列表，老版本用官方归档
cd /tmp && curl -sSL --connect-timeout 15 -o maven.tar.gz \
  "https://archive.apache.org/dist/maven/maven-3/3.9.6/binaries/apache-maven-3.9.6-bin.tar.gz"
mkdir -p /opt/maven && tar -xzf maven.tar.gz -C /opt/maven
```

## 3. Maven 阿里云镜像（否则中央仓库拉不动）

```bash
mkdir -p ~/.m2 && cat > ~/.m2/settings.xml << 'EOF'
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors>
    <mirror>
      <id>aliyun</id>
      <mirrorOf>central</mirrorOf>
      <url>https://maven.aliyun.com/repository/public</url>
    </mirror>
  </mirrors>
</settings>
EOF
```

## 4. 环境变量（每个 shell 会话设置，或写入 ~/.zshrc）

```bash
export JAVA_HOME=/opt/java/jdk-17.0.20.1+1
export PATH=$JAVA_HOME/bin:/opt/maven/apache-maven-3.9.6/bin:$PATH
mvn -v   # 验证 Java 17 + Maven 3.9.6
```

## 5. 前端依赖（node_modules 丢失时）

```bash
cd /workspace/perf-web
npm install --registry=https://registry.npmmirror.com
npx vue-tsc -b   # 验证类型检查通过
```

## 6. 验证基线完好

```bash
cd /workspace
mvn test        # 约 102 个测试全过
cd perf-web && npm run build   # vue-tsc + vite build 通过
```

## 7. 已知坑（M0–M3 期间踩过）

| 坑 | 规避 |
|---|---|
| tuna Adoptium 目录 404 或文件名变化 | 先请求目录 HTML `grep -o` 文件名再下载，别硬编码旧文件名 |
| tuna `apache/maven/maven-3/3.10.0` 不存在（目录只列最新） | 老版本走 `archive.apache.org/dist/maven/` |
| `mvn -pl perf-server test` 报找不到依赖 | 必须带 `-am`（先构建依赖模块 perf-datagen 等） |
| Spring MockHttpServletResponse 编码 API | `setDefaultCharacterEncoding(String)` 而非 `setCharacterEncoding`（后者 JDK21+ 才有重载） |
| ByteBuddy 匹配接口实现类 | 用 `hasSuperType(named(...))` 而非 `isSubTypeOf`（后者要求直接超类） |
| jjwt 0.12.x API | `Jwts.parser().verifyWith(key).build().parseSignedClaims()`（旧教程的 `parserBuilder()` 已废弃） |
