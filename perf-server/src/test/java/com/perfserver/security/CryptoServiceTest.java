package com.perfserver.security;

import org.junit.jupiter.api.Test;

import java.util.Base64;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * CryptoService 单元测试：加解密往返、存量明文兼容、脏数据兜底、脱敏。
 */
class CryptoServiceTest {

    private static CryptoService service(String secret) {
        CryptoService crypto = new CryptoService();
        crypto.setSecret(secret);
        return crypto;
    }

    @Test
    void encryptDecryptRoundTrip() {
        CryptoService crypto = service("perf-unit-test-secret");
        String cipher = crypto.encrypt("root123");
        assertTrue(cipher.startsWith(CryptoService.PREFIX), "密文应带 ENC: 前缀");
        assertNotEquals("root123", cipher);
        assertEquals("root123", crypto.decrypt(cipher));
    }

    @Test
    void encryptUsesRandomIvSoCiphertextDiffers() {
        CryptoService crypto = service("perf-unit-test-secret");
        assertNotEquals(crypto.encrypt("same-password"), crypto.encrypt("same-password"));
    }

    @Test
    void supportsUnicodeAndLongCredentials() {
        CryptoService crypto = service("perf-unit-test-secret");
        String sshKey = "-----BEGIN RSA PRIVATE KEY-----\n中文注释\n" + "x".repeat(2000);
        assertEquals(sshKey, crypto.decrypt(crypto.encrypt(sshKey)));
    }

    @Test
    void passthroughWhenSecretNotConfigured() {
        CryptoService crypto = service("");
        assertFalse(crypto.isEnabled());
        // 未配置密钥：加解密透传，保证内网演示不被阻断
        assertEquals("plain", crypto.encrypt("plain"));
        assertEquals("plain", crypto.decrypt("plain"));
    }

    @Test
    void returnsLegacyPlaintextWhenNoPrefix() {
        CryptoService crypto = service("perf-unit-test-secret");
        // 存量明文（无 ENC: 前缀）必须原样返回，否则老数据全部解密失败
        assertEquals("legacy-plain-pwd", crypto.decrypt("legacy-plain-pwd"));
        assertEquals("", crypto.decrypt(""));
        assertNull(crypto.decrypt(null));
    }

    @Test
    void fallsBackToRawValueOnCorruptedCiphertext() {
        CryptoService crypto = service("perf-unit-test-secret");
        // 非 Base64 脏数据
        assertEquals("ENC:not-!-base64!!", crypto.decrypt("ENC:not-!-base64!!"));
        // 长度不足（<= IV 长度）
        assertEquals("ENC:AAAA", crypto.decrypt("ENC:AAAA"));
        // GCM 校验失败：密文被篡改
        String cipher = crypto.encrypt("secret");
        String body = cipher.substring(CryptoService.PREFIX.length());
        byte[] packed = Base64.getDecoder().decode(body);
        packed[packed.length - 1] ^= 0x01;
        String tampered = CryptoService.PREFIX + Base64.getEncoder().encodeToString(packed);
        assertEquals(tampered, crypto.decrypt(tampered), "篡改密文不得抛异常，按原值降级处理");
    }

    @Test
    void fallsBackWhenKeyRotated() {
        String cipher = service("key-v1").encrypt("secret");
        // 密钥轮换后旧密文解密失败：降级为原值，不中断连接建立
        assertEquals(cipher, service("key-v2").decrypt(cipher));
    }

    @Test
    void maskAndHasSecret() {
        CryptoService crypto = service("perf-unit-test-secret");
        assertEquals(CryptoService.MASK, crypto.mask(crypto.encrypt("pwd")));
        assertEquals("", crypto.mask(""));
        assertEquals("", crypto.mask(null));
        assertTrue(crypto.hasSecret("anything"));
        assertFalse(crypto.hasSecret(""));
        assertFalse(crypto.hasSecret(null));
    }

    @Test
    void emptyValueIsNotEncrypted() {
        CryptoService crypto = service("perf-unit-test-secret");
        assertEquals("", crypto.encrypt(""));
        assertNull(crypto.encrypt(null));
    }
}
