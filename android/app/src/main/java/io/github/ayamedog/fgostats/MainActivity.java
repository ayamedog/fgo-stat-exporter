package io.github.ayamedog.fgostats;

import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.*;
import android.webkit.*;
import android.widget.TextView;
import androidx.webkit.*;
import com.chaquo.python.*;
import com.chaquo.python.android.AndroidPlatform;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;

/** 本机页面与安卓文件选择器之间的窄接口，不对外提供服务地址。 */
public final class MainActivity extends Activity {
    private static final int OPEN_FILE = 10, SAVE_FILE = 11, GALLERY_PERMISSION = 12;
    private static final String SOURCE_REPOSITORY = "https://github.com/ayamedog/fgo-stat-exporter";
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private WebView web;
    private PyObject runtime;
    private String origin;
    private ValueCallback<Uri[]> chooseFile;
    private SaveRequest saving;
    private SaveRequest galleryPermission;
    private boolean galleryBusy;

    private static final class SaveRequest {
        final byte[] bytes;
        final String id;
        final JavaScriptReplyProxy reply;
        final String name;
        SaveRequest(byte[] data, String filename, String requestId, JavaScriptReplyProxy proxy) {
            bytes = data; name = filename; id = requestId; reply = proxy;
        }
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        TextView waiting = new TextView(this);
        waiting.setText(R.string.preparing);
        waiting.setTextSize(18); waiting.setPadding(32, 64, 32, 32);
        setContentView(waiting);
        worker.execute(() -> {
            String stage = "RESOURCE_PREPARE_FAILED";
            try {
                File resources = ResourceInstaller.prepare(this);
                stage = "PYTHON_START_FAILED";
                if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
                stage = "LOCAL_START_FAILED";
                String profile = null;
                // 应用级固定参数由构建时输入；账号扫码凭据不在这个文件中。
                try (InputStream input = getAssets().open("game-protocol.json")) {
                    profile = new String(ResourceInstaller.readBounded(input, 16384), StandardCharsets.UTF_8);
                } catch (FileNotFoundException missing) { }
                runtime = Python.getInstance().getModule("fgo_local_exporter.mobile")
                    .get("MobileRuntime").call(getFilesDir().getPath(), resources.getPath(), profile);
                origin = runtime.callAttr("origin").toString();
                runOnUiThread(this::openPage);
            } catch (Exception error) {
                final String code = stage;
                runOnUiThread(() -> waiting.setText(getString(R.string.start_failed, code)));
            }
        });
    }

    @android.annotation.SuppressLint("SetJavaScriptEnabled")
    private void openPage() {
        if (isFinishing() || isDestroyed()) return;
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            TextView message = new TextView(this);
            message.setText(R.string.webview_update);
            message.setTextSize(18); message.setPadding(32, 64, 32, 32);
            setContentView(message); return;
        }
        web = new WebView(this);
        WebView.setWebContentsDebuggingEnabled(false);
        WebSettings settings = web.getSettings();
        // 只加载本机固定页面，统计图片合成和受限消息桥需要脚本支持。
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(false);
        settings.setDatabaseEnabled(false);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        settings.setSaveFormData(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setGeolocationEnabled(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSupportMultipleWindows(false);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !local(request.getUrl());
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (local(uri) || "data".equals(uri.getScheme()) || "blob".equals(uri.getScheme())) return null;
                return new WebResourceResponse("text/plain", "UTF-8", 403, "Blocked",
                    Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
            }
            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, android.net.http.SslError error) {
                handler.cancel();
            }
            @Override public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                view.destroy();
                TextView message = new TextView(MainActivity.this);
                message.setText(R.string.page_stopped);
                message.setTextSize(18); setContentView(message);
                worker.execute(() -> runtime.callAttr("clear")); return true;
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> result, FileChooserParams params) {
                if (chooseFile != null) chooseFile.onReceiveValue(null);
                chooseFile = result;
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE); intent.setType("*/*");
                try { startActivityForResult(intent, OPEN_FILE); }
                catch (ActivityNotFoundException error) { chooseFile.onReceiveValue(null); chooseFile = null; }
                return true;
            }
            @Override public void onPermissionRequest(PermissionRequest request) { request.deny(); }
            @Override public boolean onConsoleMessage(ConsoleMessage message) { return true; }
        });
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
          WebViewCompat.addWebMessageListener(web, "FgoNative", Collections.singleton(origin),
            (view, message, sourceOrigin, mainFrame, reply) -> {
                if (!mainFrame || !origin.equals(sourceOrigin.toString())) return;
                String raw = message.getData();
                if (raw == null || raw.length() > 29 * 1024 * 1024) return;
                worker.execute(() -> command(raw, reply));
            });
        }
        setContentView(web); web.loadUrl(origin + "/");
    }

    private boolean local(Uri uri) {
        Uri own = Uri.parse(origin);
        return "http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
            && own.getPort() == uri.getPort() && uri.getUserInfo() == null;
    }

    private void command(String raw, JavaScriptReplyProxy reply) {
        String id = "";
        try {
            JSONObject request = new JSONObject(raw);
            id = request.getString("id");
            if (!id.matches("[0-9]{1,12}")) return;
            String method = request.getString("method");
            JSONArray args = request.getJSONArray("args");
            switch (method) {
                case "connect":
                    runtime.callAttr("connect", args.getString(0)); break;
                case "status": {
                    JSONObject response = new JSONObject(runtime.callAttr("status").toString());
                    response.put("id", id); post(reply, response); return;
                }
                case "prepare_resources": runtime.callAttr("prepare_resources"); break;
                case "open_repository":
                    if (args.length() != 0) throw new IllegalArgumentException();
                    openRepository(id, reply); return;
                case "save_json":
                    save(runtime.callAttr("export_json").toJava(byte[].class), "fgo-statistics.json", "application/json", id, reply); return;
                case "save_image": {
                    String name = args.getString(1);
                    if (!name.matches("fgo-(cn-(ios|android)|jp)-report(-[0-9]{2})?\\.jpg"))
                        throw new IllegalArgumentException();
                    byte[] bytes = runtime.callAttr("image_bytes", args.getString(0), args.getString(2)).toJava(byte[].class);
                    saveGallery(bytes, name, id, reply); return;
                }
                default: throw new IllegalArgumentException();
            }
            post(reply, new JSONObject().put("id", id).put("ok", true));
        } catch (Exception error) { failed(reply, id, "LOCAL_OPERATION_FAILED"); }
    }

    private void save(byte[] data, String name, String mime, String id, JavaScriptReplyProxy reply) {
        runOnUiThread(() -> {
            if (saving != null || isFinishing()) { failed(reply, id, "SAVE_BUSY"); return; }
            saving = new SaveRequest(data, name, id, reply);
            SaveRequest pending = saving;
            int remaining = Math.max(1, Math.min(120, runtime.callAttr("remaining_seconds").toInt()));
            new Handler(Looper.getMainLooper()).postDelayed(() -> {
                if (saving == pending) {
                    Arrays.fill(pending.bytes, (byte) 0); saving = null;
                    failed(pending.reply, pending.id, "SAVE_EXPIRED");
                }
            }, remaining * 1000L);
            Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE); intent.setType(mime);
            intent.putExtra(Intent.EXTRA_TITLE, name);
            try { startActivityForResult(intent, SAVE_FILE); }
            catch (ActivityNotFoundException error) { saving = null; failed(reply, id, "FILE_PICKER_UNAVAILABLE"); }
        });
    }

    private void saveGallery(byte[] data, String name, String id, JavaScriptReplyProxy reply) {
        SaveRequest request = new SaveRequest(data, name, id, reply);
        int seconds = Math.max(1, Math.min(120, runtime.callAttr("remaining_seconds").toInt()));
        runOnUiThread(() -> {
            if (isFinishing() || galleryPermission != null || galleryBusy) {
                Arrays.fill(data, (byte) 0); failed(reply, id, "SAVE_BUSY"); return;
            }
            if (Build.VERSION.SDK_INT <= 28 && checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                galleryPermission = request;
                new Handler(Looper.getMainLooper()).postDelayed(() -> {
                    if (galleryPermission == request) {
                        galleryPermission = null; Arrays.fill(data, (byte) 0);
                        failed(reply, id, "SAVE_EXPIRED");
                    }
                }, seconds * 1000L);
                requestPermissions(new String[] {android.Manifest.permission.WRITE_EXTERNAL_STORAGE}, GALLERY_PERMISSION);
            } else {
                galleryBusy = true;
                worker.execute(() -> writeGallery(request));
            }
        });
    }

    private void writeGallery(SaveRequest request) {
        try {
            if (runtime.callAttr("remaining_seconds").toInt() <= 0)
                throw new IllegalStateException();
            GalleryWriter.save(this, request.bytes, request.name);
            post(request.reply, new JSONObject().put("id", request.id).put("ok", true)
                .put("saved", true).put("location", "gallery"));
            runOnUiThread(() -> android.widget.Toast.makeText(this, R.string.gallery_saved, android.widget.Toast.LENGTH_LONG).show());
        } catch (Exception error) { failed(request.reply, request.id, "PHOTO_SAVE_FAILED"); }
        finally {
            Arrays.fill(request.bytes, (byte) 0);
            runOnUiThread(() -> galleryBusy = false);
        }
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if (request != GALLERY_PERMISSION || galleryPermission == null) return;
        SaveRequest pending = galleryPermission; galleryPermission = null;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            galleryBusy = true;
            worker.execute(() -> writeGallery(pending));
        } else {
            Arrays.fill(pending.bytes, (byte) 0);
            failed(pending.reply, pending.id, "PHOTO_PERMISSION_DENIED");
        }
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == OPEN_FILE && chooseFile != null) {
            Uri uri = result == RESULT_OK && data != null ? data.getData() : null;
            chooseFile.onReceiveValue(uri == null ? null : new Uri[] {uri}); chooseFile = null;
        } else if (request == SAVE_FILE && saving != null) {
            SaveRequest current = saving; saving = null;
            Uri uri = result == RESULT_OK && data != null ? data.getData() : null;
            worker.execute(() -> {
                try {
                    if (uri != null) {
                        try (OutputStream output = getContentResolver().openOutputStream(uri, "wt")) {
                            if (output == null) throw new IOException();
                            output.write(current.bytes); output.flush();
                        }
                    }
                    post(current.reply, new JSONObject().put("id", current.id).put("ok", true).put("saved", uri != null));
                } catch (Exception error) { failed(current.reply, current.id, "FILE_SAVE_FAILED"); }
                finally { Arrays.fill(current.bytes, (byte) 0); }
            });
        }
    }

    private void post(JavaScriptReplyProxy reply, JSONObject value) {
        runOnUiThread(() -> {
            if (!isFinishing() && web != null && WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER))
                reply.postMessage(value.toString());
        });
    }
    private void openRepository(String id, JavaScriptReplyProxy reply) {
        runOnUiThread(() -> {
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(SOURCE_REPOSITORY)));
                post(reply, new JSONObject().put("id", id).put("ok", true));
            } catch (ActivityNotFoundException | SecurityException error) {
                failed(reply, id, "BROWSER_UNAVAILABLE");
            } catch (JSONException error) {
                failed(reply, id, "LOCAL_OPERATION_FAILED");
            }
        });
    }
    private void failed(JavaScriptReplyProxy reply, String id, String code) {
        try { post(reply, new JSONObject().put("id", id).put("ok", false).put("error", code)); }
        catch (JSONException ignored) { }
    }

    @Override public void onBackPressed() {
        new AlertDialog.Builder(this).setMessage(R.string.exit_question)
            .setPositiveButton(R.string.exit_confirm, (dialog, which) -> finish())
            .setNegativeButton(R.string.continue_use, null).show();
    }

    @Override protected void onDestroy() {
        if (chooseFile != null) chooseFile.onReceiveValue(null);
        if (saving != null) Arrays.fill(saving.bytes, (byte) 0);
        if (galleryPermission != null) Arrays.fill(galleryPermission.bytes, (byte) 0);
        galleryPermission = null;
        saving = null;
        if (web != null) { web.stopLoading(); web.clearCache(true); web.destroy(); web = null; }
        worker.execute(() -> { if (runtime != null) runtime.callAttr("close"); });
        worker.shutdown(); super.onDestroy();
    }
}
