package com.perftestor.datagen.model;

/**
 * HTTP 接口定位：AST 解析 Controller 产出的方法级坐标。
 *
 * <p>字段语义必须与宿主正则实现 {@code EndpointScanner} 的产出
 * {@code {httpMethod, path, className, classFile, methodName, line}} 完全一致，
 * 否则按 {@code (httpMethod, path)} 与前端选中项 join 时会失配。</p>
 *
 * <p>注意 {@code className} 是 <b>simpleName（文件名去 .java）</b>，不是 FQCN。</p>
 */
public class EndpointRef {

    private String httpMethod;
    private String path;
    /** simpleName（非 FQCN），与 EndpointScanner 一致 */
    private String className;
    private String methodName;
    /** 声明该接口的源码文件相对路径 */
    private String declaringFile;
    private int line;

    public EndpointRef() {
    }

    public EndpointRef(String httpMethod, String path, String className,
                       String methodName, String declaringFile, int line) {
        this.httpMethod = httpMethod;
        this.path = path;
        this.className = className;
        this.methodName = methodName;
        this.declaringFile = declaringFile;
        this.line = line;
    }

    public String getHttpMethod() {
        return httpMethod;
    }

    public void setHttpMethod(String httpMethod) {
        this.httpMethod = httpMethod;
    }

    public String getPath() {
        return path;
    }

    public void setPath(String path) {
        this.path = path;
    }

    public String getClassName() {
        return className;
    }

    public void setClassName(String className) {
        this.className = className;
    }

    public String getMethodName() {
        return methodName;
    }

    public void setMethodName(String methodName) {
        this.methodName = methodName;
    }

    public String getDeclaringFile() {
        return declaringFile;
    }

    public void setDeclaringFile(String declaringFile) {
        this.declaringFile = declaringFile;
    }

    public int getLine() {
        return line;
    }

    public void setLine(int line) {
        this.line = line;
    }

    /** 匹配键：与方法无关的精确匹配键（HTTP 方法 + 路径） */
    public String key() {
        return (httpMethod == null ? "" : httpMethod.toUpperCase()) + " " + (path == null ? "" : path);
    }

    /** 兜底匹配键：忽略 HTTP 方法 */
    public String pathKey() {
        return path == null ? "" : path;
    }

    /** 最兜底匹配键：文件 + 方法名 */
    public String memberKey() {
        return (declaringFile == null ? "" : declaringFile) + "#" + (methodName == null ? "" : methodName);
    }

    @Override
    public String toString() {
        return key() + " (" + memberKey() + ":" + line + ")";
    }
}
