package io.github.ayamedog.fgostats.devtools;

import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.Key;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.cert.Certificate;
import java.util.Arrays;
import java.util.HexFormat;

/** 开发签名前核验工具，不放入 APK，不输出签名凭据。 */
public final class VerifySigningCertificate {
    private static String required(String name) {
        String value = System.getenv(name);
        if (value == null || value.isEmpty()) throw new IllegalArgumentException("SIGNING_INPUT_MISSING");
        return value;
    }

    public static void main(String[] args) {
        try {
            verify(args);
        } catch (Exception failure) {
            System.err.println("SIGNING_VALIDATION_FAILED");
            System.exit(1);
        }
    }

    private static void verify(String[] args) throws Exception {
        if (args.length != 1 || !args[0].matches("[0-9a-f]{64}"))
            throw new IllegalArgumentException("EXPECTED_CERT_INVALID");
        char[] storePassword = required("FGO_ANDROID_STORE_PASSWORD").toCharArray();
        char[] keyPassword = required("FGO_ANDROID_KEY_PASSWORD").toCharArray();
        try {
            Path path = Path.of(required("FGO_ANDROID_KEYSTORE"));
            String alias = required("FGO_ANDROID_KEY_ALIAS");
            KeyStore store = KeyStore.getInstance("PKCS12");
            try (InputStream source = Files.newInputStream(path)) { store.load(source, storePassword); }
            if (!store.isKeyEntry(alias)) throw new IllegalStateException("SIGNING_KEY_UNAVAILABLE");
            Certificate cert = store.getCertificate(alias);
            Key key = store.getKey(alias, keyPassword);
            if (cert == null || !(key instanceof PrivateKey)) throw new IllegalStateException("SIGNING_KEY_UNAVAILABLE");
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(cert.getEncoded());
            if (!MessageDigest.isEqual(digest, HexFormat.of().parseHex(args[0])))
                throw new IllegalStateException("SIGNING_CERTIFICATE_MISMATCH");
            System.out.println("SIGNING_CERTIFICATE_MATCHED");
        } finally {
            Arrays.fill(storePassword, '\0');
            Arrays.fill(keyPassword, '\0');
        }
    }
}
