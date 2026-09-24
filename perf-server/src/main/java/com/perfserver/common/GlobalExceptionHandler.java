package com.perfserver.common;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/**
 * 全局异常处理：参数校验失败返回业务错误码，其余异常记录日志并返回通用错误。
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    private final Messages messages;

    public GlobalExceptionHandler(Messages messages) {
        this.messages = messages;
    }

    /** 参数校验失败（IllegalArgumentException） */
    @ExceptionHandler(IllegalArgumentException.class)
    public ApiResponse<Void> handleIllegalArgument(IllegalArgumentException e) {
        return ApiResponse.error(e.getMessage());
    }

    /** 其他异常 */
    @ExceptionHandler(Exception.class)
    public ApiResponse<Void> handleException(Exception e) {
        log.error("unexpected error", e);
        return ApiResponse.error(messages.get("common.error.internal", e.getMessage()));
    }
}
