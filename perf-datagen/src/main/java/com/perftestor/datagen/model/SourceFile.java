package com.perftestor.datagen.model;

/**
 * 源码文件抽象：模块只认"文件集合"，不关心来源。
 * 双模式供给——磁盘目录自足遍历，或宿主源码索引（如 SourceFileService）适配产物。
 */
public class SourceFile {

    /** 相对或绝对路径（用于提示来源定位 文件:行号） */
    private final String path;
    /** 文本内容（解析失败/超大文件由宿主截断） */
    private final String content;

    public SourceFile(String path, String content) {
        this.path = path;
        this.content = content;
    }

    public String getPath() {
        return path;
    }

    public String getContent() {
        return content;
    }
}
