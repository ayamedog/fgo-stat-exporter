"""在纯 Java 中以人工 HTTPS 响应验证 Android 验证码资源限制。"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_android_captcha_loader_restricts_network_and_redirects(tmp_path):
    javac, java = shutil.which("javac"), shutil.which("java")
    if not javac or not java:
        pytest.skip("Java compiler and runtime are needed for this Android test")
    package = tmp_path / "io/github/ayamedog/fgostats"
    package.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "android/app/src/main/java/io/github/ayamedog/fgostats/CaptchaResourceLoader.java"
    )
    harness = package / "CaptchaLoaderProbe.java"
    harness.write_text(
        r'''package io.github.ayamedog.fgostats;
import java.net.*;
import java.io.*;
import java.security.cert.Certificate;
import javax.net.ssl.HttpsURLConnection;

public class CaptchaLoaderProbe {
    static int opened;
    static class Connection extends HttpsURLConnection {
        Connection(URL u) { super(u); opened++; }
        public void connect() { }
        public void disconnect() { }
        public boolean usingProxy() { return false; }
        public String getCipherSuite() { return "test"; }
        public Certificate[] getLocalCertificates() { return null; }
        public Certificate[] getServerCertificates() { return null; }
        public int getResponseCode() {
            if (url.getPath().startsWith("/redirect")) return 302;
            return 200;
        }
        public String getHeaderField(String name) {
            if (name.equals("Location")) {
                if (url.getPath().equals("/redirect-loop")) return "/redirect-loop";
                return url.getPath().equals("/redirect-safe")
                    ? "https://api.geetest.com/ok" : "https://evil.example/collect";
            }
            if (name.equals("Access-Control-Allow-Origin")) return "null";
            if (name.equals("Set-Cookie")) return "synthetic-cookie=must-not-forward";
            return null;
        }
        public String getContentType() { return "application/javascript; charset=UTF-8"; }
        public long getContentLengthLong() {
            if (url.getPath().equals("/stream-large")) return -1;
            return url.getPath().equals("/large") ? 5L * 1024 * 1024 : 2;
        }
        public InputStream getInputStream() {
            if (getRequestProperty("Cookie") != null || getRequestProperty("Authorization") != null
                    || getRequestProperty("Referer") != null
                    || !"null".equals(getRequestProperty("Origin"))) throw new AssertionError();
            if (url.getPath().equals("/stream-large")) {
                return new InputStream() {
                    int left = 5 * 1024 * 1024;
                    public int read() { if (left-- > 0) return 0; return -1; }
                };
            }
            return new ByteArrayInputStream(new byte[] {1, 2});
        }
    }
    static void check(boolean result) { if (!result) throw new AssertionError(); }
    static CaptchaResourceLoader.Resource get(String path) {
        return CaptchaResourceLoader.fetch("https://static.geetest.com" + path,
            "GET", false, "synthetic-browser");
    }
    public static void main(String[] args) throws Exception {
        URL.setURLStreamHandlerFactory(protocol ->
            "https".equals(protocol) ? new URLStreamHandler() {
            protected URLConnection openConnection(URL url) { return new Connection(url); }
        } : null);
        check(CaptchaResourceLoader.allowed(new URI("https://api.geetest.com/path?a=1")));
        for (String bad : new String[] {"http://api.geetest.com/a", "https://api.geetest.com.evil.example/a",
                "https://evil.example@api.geetest.com/a", "https://api.geetest.com:444/a",
                "https://api.geetest.com/a#fragment", "https://127.0.0.1/a"})
            check(!CaptchaResourceLoader.allowed(new URI(bad)));
        check(CaptchaResourceLoader.fetch("https://static.geetest.com/x", "POST", false, null)
            == null);
        check(CaptchaResourceLoader.fetch("https://static.geetest.com/x", "GET", true, null)
            == null);
        check(opened == 0);
        CaptchaResourceLoader.Resource ok = get("/ok");
        check(ok != null && ok.body.length == 2 && "application/javascript".equals(ok.mime)
                && "UTF-8".equals(ok.encoding)
                && "null".equals(ok.headers.get("Access-Control-Allow-Origin"))
                && !ok.headers.containsKey("Set-Cookie"));
        int count = opened;
        check(get("/redirect-evil") == null && opened == count + 1);
        count = opened;
        check(get("/redirect-safe") != null && opened == count + 2);
        count = opened;
        check(get("/redirect-loop") == null && opened == count + 4);
        check(get("/large") == null);
        check(get("/stream-large") == null);
        CookieHandler.setDefault(new CookieManager());
        count = opened;
        check(get("/ok") == null && opened == count);
        CookieHandler.setDefault(null);
    }
}
''',
        encoding="utf-8",
    )
    subprocess.run(
        [javac, "-encoding", "UTF-8", "-d", str(tmp_path), str(source), str(harness)], check=True
    )
    subprocess.run(
        [java, "-cp", str(tmp_path), "io.github.ayamedog.fgostats.CaptchaLoaderProbe"], check=True
    )
