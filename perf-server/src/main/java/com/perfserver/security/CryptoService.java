package com.perfserver.security;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import javax.crypto.Cipher;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;

/**
 * 敏感凭据加解密服务：AES-256-GCM，密钥取自环境变量（{@code PERF_CRYPTO_SECRET}）。
 *
 * <p>落库格式：{@code ENC:} + Base64(12 字节随机 IV ‖ 密文)。GCM 自带完整性校验，
 * 密文被篡改会直接解密失败而非产出脏明文。
 *
 * <p><b>前缀兼容式平滑迁移</b>：存量库里已有明文凭据，强制解密会直接报错。
 * 因此 {@link #decrypt} 对「无 {@code ENC:} 前缀」或「解密失败」的值一律原样返回，
 * 写入路径统一加密——无需数据迁移脚本，存量明文在下次保存时自动转为密文。
 *
 * <p>密钥未配置时（{@code enabled=false}）加解密退化为原样透传，
 * 保证内网演示环境与未配置密钥的部署不被阻断。
 */
@Service
public class CryptoService {

    private static final Logger log = LoggerFactory.getLogger(CryptoService.class);

    /** 密文前缀：用于区分加密值与存量明文 */
    public static final String PREFIX = "ENC:";
    /** 展示脱敏占位符 */
    public static final String MASK = "******";

    private static final String ALGORITHM = "AES/GCM/NoPadding";
    private static final int IV_LENGTH = 12;
    private static final int TAG_BITS = 128;

    private final SecureRandom random = new SecureRandom();

    /**
     * 主密钥口令（任意长度，内部 SHA-256 归一化为 32 字节）。
     * 为空表示未启用加密，加解密透传。
     */
    private String secret = "";

    private volatile SecretKey key;

    /** 密钥注入点：{@code perf.crypto.secret}（环境变量 PERF_CRYPTO_SECRET） */
    @Value("${perf.crypto.secret:}")
    public void setSecret(String secret) {
        this.secret = secret == null ? "" : secret;
        this.key = null;
    }

    /** 是否启用加密（密钥非空即启用） */
    public boolean isEnabled() {
        return !secret.isEmpty();
    }

    /**
     * 加密明文凭据。
     *
     * @param plain 明文（null/空原样返回）
     * @return {@code ENC:...} 密文；未启用加密时原样返回
     */
    public String encrypt(String plain) {
        if (plain == null || plain.isEmpty() || !isEnabled()) {
            return plain;
        }
        try {
            byte[] iv = new byte[IV_LENGTH];
            random.nextBytes(iv);
            Cipher cipher = Cipher.getInstance(ALGORITHM);
            cipher.init(Cipher.ENCRYPT_MODE, key(), new GCMParameterSpec(TAG_BITS, iv));
            byte[] ciphertext = cipher.doFinal(plain.getBytes(StandardCharsets.UTF_8));
            byte[] packed = new byte[iv.length + ciphertext.length];
            System.arraycopy(iv, 0, packed, 0, iv.length);
            System.arraycopy(ciphertext, 0, packed, iv.length, ciphertext.length);
            return PREFIX + Base64.getEncoder().encodeToString(packed);
        } catch (Exception e) {
            // 加密失败不得让凭据以明文落库之外的形式丢失：抛异常中断保存，由调用方提示用户
            throw new IllegalStateException("凭据加密失败: " + e.getMessage(), e);
        }
    }

    /**
     * 解密凭据。
     *
     * <p>以下情况原样返回入参，不抛异常：值为 null/空、无 {@code ENC:} 前缀（存量明文）、
     * Base64 非法、IV 长度不足、GCM 校验失败（密钥轮换后的旧密文或脏数据）。
     * 保证历史数据与密钥变更不会中断连接建立流程。
     *
     * @param stored 库中存储值（可能是密文、也可能是存量明文）
     * @return 明文
     */
    public String decrypt(String stored) {
        if (stored == null || stored.isEmpty() || !stored.startsWith(PREFIX)) {
            return stored;
        }
        try {
            byte[] packed = Base64.getDecoder().decode(stored.substring(PREFIX.length()));
            if (packed.length <= IV_LENGTH) {
                log.warn("凭据密文长度异常（{} 字节），按原值处理", packed.length);
                return stored;
            }
            byte[] iv = new byte[IV_LENGTH];
            System.arraycopy(packed, 0, iv, 0, IV_LENGTH);
            byte[] ciphertext = new byte[packed.length - IV_LENGTH];
            System.arraycopy(packed, IV_LENGTH, ciphertext, 0, ciphertext.length);
            Cipher cipher = Cipher.getInstance(ALGORITHM);
            cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(TAG_BITS, iv));
            return new String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8);
        } catch (Exception e) {
            // 密钥轮换后的旧密文 / 人为改库产生的脏数据：降级为原值，避免整个分析链路中断
            log.warn("凭据解密失败（可能是密钥已轮换或数据损坏），按原值处理: {}", e.getClass().getSimpleName());
            return stored;
        }
    }

    /**
     * 判断库中是否配置了凭据（用于脱敏后前端区分「已配置/未配置」）。
     *
     * @param stored 库中存储值
     * @return 非空即视为已配置
     */
    public boolean hasSecret(String stored) {
        return stored != null && !stored.isEmpty();
    }

    /**
     * 脱敏展示值：已配置返回 {@link #MASK}，未配置返回空串。
     *
     * @param stored 库中存储值
     * @return 脱敏后的展示值
     */
    public String mask(String stored) {
        return hasSecret(stored) ? MASK : "";
    }

    /** 懒加载：口令任意长度，SHA-256 归一化为 32 字节 AES 密钥 */
    private SecretKey key() throws Exception {
        SecretKey cached = key;
        if (cached == null) {
            synchronized (this) {
                cached = key;
                if (cached == null) {
                    MessageDigest digest = MessageDigest.getInstance("SHA-256");
                    byte[] hashed = digest.digest(secret.getBytes(StandardCharsets.UTF_8));
                    cached = new SecretKeySpec(hashed, "AES");
                    key = cached;
                }
            }
        }
        return cached;
    }
}
