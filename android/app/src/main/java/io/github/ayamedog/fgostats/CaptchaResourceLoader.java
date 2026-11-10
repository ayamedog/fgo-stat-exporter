package io.github.ayamedog.fgostats;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.CookieHandler;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URISyntaxException;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import javax.net.ssl.HttpsURLConnection;

/** 极验资源只走固定 HTTPS 域名；调用方应始终禁止验证码获得页面同源权限。 */
final class CaptchaResourceLoader {
    private static final Set<String> HOSTS = Collections.unmodifiableSet(new HashSet<>(Arrays.asList(
        "static.geetest.com", "api.geetest.com", "static.geevisit.com", "monitor.geetest.com"
    )));
    private static final int MAX_URL = 16384, MAX_BYTES = 4 * 1024 * 1024;
    private static final int MAX_REDIRECTS = 3, WAIT_SECONDS = 5;
    private static final Semaphore SLOTS = new Semaphore(6);

    static final class Resource {
        final String mime;
        final String encoding;
        final Map<String, String> headers;
        final byte[] body;
        Resource(String mime, String encoding, Map<String, String> headers, byte[] body) {
            this.mime = mime;
            this.encoding = encoding;
            this.headers = headers;
            this.body = body;
        }
    }

    private CaptchaResourceLoader() { }

    static boolean allowed(URI uri) {
        if (uri == null || uri.toASCIIString().length() > MAX_URL || uri.isOpaque()
                || !"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null
                || !HOSTS.contains(uri.getHost().toLowerCase(Locale.ROOT))
                || uri.getUserInfo() != null || uri.getFragment() != null
                || (uri.getPort() != -1 && uri.getPort() != 443)) return false;
        String path = uri.getRawPath();
        return path != null && path.startsWith("/") && !uri.toString().contains("\\");
    }

    /** 只转发合成的请求头；不读取、转发或保存网页 Cookie 和认证头。 */
    static Resource fetch(String url, String method, boolean mainFrame, String userAgent) {
        if (mainFrame || !"GET".equals(method) || CookieHandler.getDefault() != null) return null;
        boolean acquired = false;
        try {
            URI current = new URI(url);
            if (!allowed(current)) return null;
            acquired = SLOTS.tryAcquire(WAIT_SECONDS, TimeUnit.SECONDS);
            if (!acquired) return null;
            for (int redirects = 0; redirects <= MAX_REDIRECTS; redirects++) {
                HttpsURLConnection connection = (HttpsURLConnection) current.toURL().openConnection();
                try {
                    connection.setInstanceFollowRedirects(false);
                    connection.setUseCaches(false);
                    connection.setConnectTimeout(10000);
                    connection.setReadTimeout(15000);
                    connection.setRequestMethod("GET");
                    connection.setRequestProperty("Accept", "*/*");
                    connection.setRequestProperty("Accept-Encoding", "identity");
                    connection.setRequestProperty("Origin", "null");
                    if (safeUserAgent(userAgent)) connection.setRequestProperty("User-Agent", userAgent);
                    int status = connection.getResponseCode();
                    if (status == 301 || status == 302 || status == 303 || status == 307 || status == 308) {
                        String location = connection.getHeaderField("Location");
                        if (redirects == MAX_REDIRECTS || location == null || location.length() > MAX_URL)
                            return null;
                        URI next = current.resolve(new URI(location));
                        if (!allowed(next)) return null;
                        current = next;
                        continue;
                    }
                    if (status != HttpURLConnection.HTTP_OK) return null;
                    long size = connection.getContentLengthLong();
                    if (size > MAX_BYTES) return null;
                    byte[] body;
                    try (InputStream input = connection.getInputStream();
                         ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                        byte[] buffer = new byte[8192];
                        int count, total = 0;
                        while ((count = input.read(buffer)) != -1) {
                            total += count;
                            if (total > MAX_BYTES) return null;
                            output.write(buffer, 0, count);
                        }
                        body = output.toByteArray();
                    }
                    String contentType = connection.getContentType();
                    String mime = "application/octet-stream", encoding = null;
                    if (contentType != null && contentType.length() <= 512) {
                        String[] pieces = contentType.split(";");
                        String candidate = pieces[0].trim().toLowerCase(Locale.ROOT);
                        if (candidate.matches("[a-z0-9.+-]+/[a-z0-9.+-]+")) mime = candidate;
                        for (int i = 1; i < pieces.length; i++) {
                            String piece = pieces[i].trim();
                            if (piece.toLowerCase(Locale.ROOT).startsWith("charset=")) {
                                String value = piece.substring(8).trim().replace("\"", "");
                                if (value.matches("[A-Za-z0-9._-]{1,40}")) encoding = value;
                            }
                        }
                    }
                    Map<String, String> headers = new HashMap<>();
                    headers.put("Cache-Control", "no-store");
                    String allowOrigin = connection.getHeaderField("Access-Control-Allow-Origin");
                    if ("*".equals(allowOrigin) || "null".equals(allowOrigin))
                        headers.put("Access-Control-Allow-Origin", allowOrigin);
                    return new Resource(mime, encoding, Collections.unmodifiableMap(headers), body);
                } finally {
                    connection.disconnect();
                }
            }
        } catch (IOException | URISyntaxException | IllegalArgumentException | SecurityException ignored) {
            // 不记录 URL、查询参数或请求内容。
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        } finally {
            if (acquired) SLOTS.release();
        }
        return null;
    }

    private static boolean safeUserAgent(String value) {
        if (value == null || value.length() > 512 || value.isEmpty()) return false;
        for (byte b : value.getBytes(StandardCharsets.UTF_8)) {
            if (b < 32 || b > 126) return false;
        }
        return true;
    }
}
