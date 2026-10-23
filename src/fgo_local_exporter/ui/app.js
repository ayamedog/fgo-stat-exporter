"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const csrf = document.querySelector('meta[name="fgo-csrf"]').content;
  const busy = new Set(["generating", "waiting_scan", "waiting_confirm", "game_handoff", "game_initialization", "fetching_statistics", "filtering_statistics"]);
  const errors = {
    LOCAL_BUSY: "当前任务较多，请稍后再试。",
    VISITOR_EXPIRED: "浏览器会话已过期，请刷新页面重新开始。",
    CONSENT_REQUIRED: "请先阅读并勾选数据处理说明。",
    OPERATION_NOT_READY: "当前状态不允许此操作，请稍候或清除本次数据后重试。",
    LOCAL_PROFILE_REQUIRED: "扫码配置缺失，请使用包含扫码配置的新版 Windows 程序；也可切换到“导入文件”。",
    LOCAL_PROFILE_INVALID: "扫码配置未通过校验，请更新程序；使用自定义配置时请检查配置文件。仍可切换到“导入文件”。",
    SERVER_SELECTION_REQUIRED: "请先选择角色所在的 iOS 或安卓服务器。",
    INPUT_TOO_LARGE: "文件为空或超过 16 MB，请选择登录响应或已过滤的统计文件。",
    IMPORT_TIMEOUT: "文件解析超时，请使用本工具导出的 JSON，或检查文件是否完整。",
    IMPORT_INVALID: "无法识别这个文件，请提供国服、日服登录响应或本工具的统计 JSON。",
    STATISTICS_LOGIN_REQUIRED: "文件不是成功的游戏登录响应，请检查是否选对文件。",
    STATISTICS_REGION_UNSUPPORTED: "目前支持 FGO 国服和日服统计文件，请检查文件所属区服。",
    STATISTICS_CORE_TABLES_MISSING: "文件缺少角色、从者或图鉴等必要统计表，请换用完整登录响应。",
    STATISTICS_NOT_READY: "统计尚未就绪或已过期，请重新扫码或导入文件。",
    REPORT_NOT_CONFIGURED: "统计资源尚未就绪，请检查或修复字典。",
    REPORT_REGION_NOT_CONFIGURED: "日服字典尚未就绪，请检查资源更新后重新生成。",
    MASTER_REGION_MISMATCH: "统计文件与字典区服不一致，请检查资源更新。",
    REPORT_ENGINE_RESTART_REQUIRED: "统计模块已更新，请重启程序后继续生成统计图。",
    REPORT_QUEUE_TIMEOUT: "报告队列暂时繁忙，请稍后重试。",
    REPORT_IMAGE_TIMEOUT: "图片下载超过一分钟，请检查网络后重新生成；已经下载的公开图片会保留。",
    REPORT_EXPIRED: "这份报告已清除或过期，请重新生成。",
    REPORT_IMAGE_MISSING: "部分统计图片尚未下载，请检查网络后重新生成。",
    REPORT_IMAGES_PARTIAL: "部分公开图片暂不可用，统计内容已生成；助战图片不全时保留文字布局，稍后重新生成可补齐图片。",
    REPORT_BUILD_FAILED: "报告未能完成，请保留错误码供排查。统计 JSON 仍可单独下载。",
    REMOTE_REQUEST_FAILED: "暂时无法连接官方服务，请稍后重新生成二维码。",
    REMOTE_TLS_VERIFY_FAILED: "连接官方服务时未能通过证书校验，请核对系统时间后重试。",
    REMOTE_TIMEOUT: "官方服务连接或响应超时，请稍后重试。",
    SDK_QR_POLL_FAILED: "授权被取消、过期或官方服务拒绝，请重新扫码。",
    REQUEST_REJECTED: "请求校验未通过，请重新启动程序。",
    NETWORK_UNAVAILABLE: "本机接口暂时不可用，请重启程序后重试。"
  };
  let mode = "qr", selectedFile = null, current = null, pending = false, qrGeneration = -1;
  let shownReport = "", imageBusy = false, blobUrls = [], frameHeight = 0;
  let countdownEndsAt = 0;
  let sessionEpoch = 0;
  const describe = code => errors[code] || (/^STAT_FIELD_[A-Z0-9_]+$/.test(code) || code === "STATISTICS_FIELD_INVALID"
    ? "统计字段格式暂不兼容，请提供下方完整错误码以便定位。"
    : "本次操作未完成，请核对文件或重新尝试。排查时只需提供错误码。");
  const server = () => document.querySelector('input[name="server"]:checked')?.value || "";
  const size = bytes => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(2)} MB` : `${(bytes / 1024).toFixed(1)} KB`;
  const remaining = seconds => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  function updateCountdown() {
    const seconds = Math.max(0, Math.ceil((countdownEndsAt - performance.now()) / 1000));
    if (current?.download_ready) $("data-remaining").textContent = remaining(seconds);
    if (current?.has_qr) $("qr-badge").textContent = current.phase === "waiting_confirm" ? "已扫码，请在手机确认" : `${current.server_name} · ${remaining(seconds)}`;
  }
  const mobileSaving = () => window.matchMedia("(pointer: coarse), (max-width: 740px)").matches;
  const date = (value, region) => value ? new Intl.DateTimeFormat("zh-CN", {timeZone:region === "JP" ? "Asia/Tokyo" : "Asia/Shanghai", year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(value * 1000)) : "未提供";

  async function api(path, options = {}) {
    try {
      const response = await fetch(path, {credentials:"same-origin", cache:"no-store", ...options,
        headers:{"X-FGO-CSRF":csrf, ...options.headers}});
      const value = await response.json();
      if (!response.ok) throw Object.assign(new Error(value.error || "REQUEST_INVALID"), {code:value.error});
      return value;
    } catch (error) {
      if (!error.code) error.code = "NETWORK_UNAVAILABLE";
      throw error;
    }
  }
  const post = (path, value) => api(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(value)});
  function showError(code) {
    $("error-box").hidden = false;
    $("error-title").textContent = "这次记录还未完成";
    $("error-message").textContent = describe(code);
    $("error-code").textContent = code;
  }
  function controls() {
    const working = pending || (current && busy.has(current.phase));
    const switchable = mode === "qr" && current?.can_switch_server && $("consent").checked;
    document.querySelectorAll('input[name="server"]').forEach(input => { input.disabled = pending || (!!working && !switchable); });
    const ready = !!server() && $("consent").checked && (mode === "qr" || selectedFile);
    const qrUnavailable = mode === "qr" && current?.qr_available === false;
    $("start-button").disabled = !!working || !ready || qrUnavailable;
    $("start-label").textContent = pending ? "正在提交…" : working ? "正在处理…" : mode === "qr" ? (current?.phase === "expired" ? "重新生成二维码" : "生成二维码") : "读取统计文件";
    $("field-hint").textContent = switchable && !pending ? "区服选错可直接切换，切换后请手动点击“生成二维码”。" : working ? "处理期间请保持此页面打开" : !server() ? "请先选择角色所在区服" : qrUnavailable ? describe(current.qr_error || "LOCAL_PROFILE_REQUIRED") : !$("consent").checked ? "请阅读并勾选数据处理说明" : mode === "file" && !selectedFile ? "请选择需要导入的文件" : "数据在本机内存中短时处理，完成后请及时保存";
    const reportWorking = ["queued", "building"].includes(current?.report_phase);
    $("build-report").disabled = pending || reportWorking || !current?.download_ready || !current?.report_available;
    $("build-report").textContent = reportWorking ? "正在生成统计图…" : current?.report_phase === "ready" ? "重新生成统计图 ↗" : "生成统计图 ↗";
    $("report-note").textContent = reportWorking ? (current.report_phase === "queued" ? "正在排队，请稍候…" : reportProgress(current.report_progress)) : current?.download_ready ? "图片在你的浏览器中合成。" : "请先扫码获取或导入数据。";
    $("download-image").disabled = imageBusy || !shownReport;
    $("download-sections").disabled = imageBusy || !shownReport;
  }
  async function changeServer() {
    const selected = server();
    if (pending || mode !== "qr" || !current?.can_switch_server || selected === current.server) { controls(); return; }
    if (!$("consent").checked) { showError("CONSENT_REQUIRED"); controls(); return; }
    pending = true; sessionEpoch++; controls();
    $("error-box").hidden = true;
    qrGeneration = -1; $("qr-image").hidden = true; $("qr-image").removeAttribute("src");
    $("qr-badge").hidden = true; $("qr-empty").hidden = false;
    $("qr-empty").querySelector("b").textContent = "正在切换区服…";
    try {
      render(await post("/api/server", {server:selected, generation:current.generation}));
    } catch (error) {
      try { render(await api("/api/state")); } catch (_refreshError) {}
      showError(error.code);
    } finally { pending = false; controls(); }
  }
  function setMode(value) {
    mode = value;
    $("server-choice").hidden = value !== "qr";
    for (const item of ["qr", "file"]) {
      $("tab-" + item).classList.toggle("selected", item === value);
      $("tab-" + item).setAttribute("aria-selected", String(item === value));
      $(item + "-pane").hidden = item !== value;
    }
    controls();
  }
  function reportProgress(value) {
    const stages = {parsing:"正在读取统计数据…", calculating:"正在计算统计…", images:"正在准备图片", support:"正在绘制助战", layout:"正在整理统计图…"};
    const text = stages[value?.stage] || "正在准备统计图…";
    return value?.total ? `${text} ${value.completed} / ${value.total}…` : text;
  }
  function discardImages() {
    blobUrls.forEach(url => URL.revokeObjectURL(url));
    blobUrls = [];
    $("image-links").replaceChildren();
    $("local-images").hidden = true;
  }
  function clearReport() {
    shownReport = "";
    $("report-viewer").hidden = true;
    $("report-frame").removeAttribute("src");
    $("image-progress").textContent = "";
    discardImages();
  }
  function render(value) {
    if (current && value.generation < current.generation) return;
    current = value;
    if (value.source === "qr" && busy.has(value.phase) && value.server) {
      document.querySelectorAll('input[name="server"]').forEach(input => { input.checked = input.value === value.server; });
    }
    countdownEndsAt = performance.now() + Math.max(0, value.remaining_seconds || 0) * 1000;
    $("status-message").textContent = ["ready", "cleared"].includes(value.phase) ? (value.phase === "cleared" ? (value.server ? "原二维码已取消，请确认区服后点击“生成二维码”。" : "本次记录已清除，可以重新扫码或导入文件。") : "选择扫码登录，或导入已有文件。") : value.message;
    $("status-line").className = "status-line" + (busy.has(value.phase) ? " busy" : value.phase === "complete" ? " complete" : value.phase === "error" ? " error" : "");
    $("live-tag").textContent = value.phase === "complete" ? "数据就绪" : busy.has(value.phase) ? "正在读取" : value.phase === "expired" ? "已过期" : "等待数据";
    $("live-tag").className = "live-tag" + (value.phase === "complete" ? " complete" : "");
    if (value.has_qr) {
      if (qrGeneration !== value.generation) {
        $("qr-image").src = `/api/qr.svg?generation=${value.generation}`;
        qrGeneration = value.generation;
      }
      $("qr-image").hidden = false; $("qr-empty").hidden = true; $("qr-badge").hidden = false;
    } else {
      $("qr-image").hidden = true; $("qr-image").removeAttribute("src");
      $("qr-empty").hidden = false; $("qr-badge").hidden = true; qrGeneration = -1;
      $("qr-empty").querySelector("b").textContent = value.phase === "expired" ? "二维码已过期" : value.phase === "cleared" && value.server ? "原二维码已取消" : "你的二维码会显示在这里";
      $("qr-empty").querySelector("p").textContent = value.phase === "cleared" && value.server ? "请点击下方“生成二维码”" : "选择区服，阅读下方说明后生成";
    }
    const complete = value.phase === "complete" && value.download_ready && value.preview;
    $("empty-result").hidden = !!complete;
    $("snapshot").hidden = !complete;
    $("export-actions").hidden = !complete;
    if (complete) {
      const p = value.preview;
      $("account-name").textContent = p.name;
      $("account-server").textContent = value.server_name + (value.source === "file" ? " · 自动识别" : "");
      $("account-level").textContent = p.level == null ? "等级未提供" : `Lv. ${p.level}`;
      $("captured-at").textContent = date(p.captured_at, p.region);
      $("table-count").textContent = `${Object.keys(p.tables).length} 张数据表`;
      $("file-size").textContent = size(p.bytes);
    }
    $("error-box").hidden = !value.error;
    if (value.error) showError(value.error);
    $("report-error").hidden = !value.report_error;
    $("report-error").textContent = value.report_error ? `${describe(value.report_error)} (${value.report_error})` : "";
    $("report-warning").hidden = !value.report_warning;
    $("report-warning").textContent = value.report_warning ? describe(value.report_warning) : "";
    if (value.report_phase === "ready" && value.report_version && shownReport !== value.report_version) {
      discardImages();
      shownReport = value.report_version;
      $("report-viewer").hidden = false;
      $("report-frame").src = `/api/report/${shownReport}/view`;
      $("viewer-meta").textContent = window.fgoNativeGallery ? "图片在本机生成，点击保存即可存入系统相册。" : window.fgoDesktopSave ? "图片在本机生成，保存时会打开系统文件选择器。" : mobileSaving() ? "图片由浏览器在你的设备上生成。保存时请长按生成后的统计图片。" : "图片由浏览器在你的设备上生成。默认保存为完整长图。";
    } else if (shownReport && (!value.report_version || !complete)) {
      clearReport();
    }
    updateCountdown(); controls();
    window.dispatchEvent(new CustomEvent("fgo-state", {detail:{ready:!!complete, generation:value.generation, engine:value.engine_revision || ""}}));
  }
  async function poll() {
    const epoch = sessionEpoch;
    try { const value = await api("/api/state"); if (epoch === sessionEpoch && !pending) render(value); }
    catch (error) { if (epoch === sessionEpoch && !pending) showError(error.code); }
    finally { window.setTimeout(poll, 2000); }
  }
  function chooseFile(file) {
    if (!file) return;
    if (!file.size || file.size > 16 * 1024 * 1024) { showError("INPUT_TOO_LARGE"); return; }
    selectedFile = file;
    $("file-name").textContent = file.name;
    $("file-detail").textContent = `${size(file.size)} · 点击可重新选择`;
    controls();
  }
  async function start() {
    if (pending || !server() || !$("consent").checked) return;
    pending = true; sessionEpoch++; controls(); $("error-box").hidden = true;
    try {
      if (mode === "qr") {
        render(await post("/api/start", {server:server(), consent:true}));
      } else if (selectedFile) {
        render(await api("/api/import", {method:"POST", headers:{"Content-Type":"application/octet-stream", "X-FGO-Consent":"yes", "X-FGO-Server":server()}, body:selectedFile}));
        selectedFile = null; $("capture-file").value = "";
        $("file-name").textContent = "文件已提交，可在完成后重新选择";
        $("file-detail").textContent = "原始文件仅作临时解析";
      }
    } catch (error) { showError(error.code); }
    finally { pending = false; controls(); }
  }
  async function build() {
    pending = true; controls();
    try { render(await post("/api/report", {friend:$("show-friend").checked, support:$("show-support").checked})); }
    catch (error) { $("report-error").hidden = false; $("report-error").textContent = `${describe(error.code)} (${error.code})`; }
    finally { pending = false; controls(); }
  }
  function fitFrame() {
    if (!shownReport || !frameHeight) return;
    const width = $("preview-scroll").clientWidth - (window.innerWidth < 740 ? 14 : 32);
    const scale = Math.min(1, width / 1200);
    $("report-frame").style.transform = `scale(${scale})`;
    $("report-frame").style.height = `${frameHeight}px`;
    $("preview-stage").style.width = `${1200 * scale}px`;
    $("preview-stage").style.height = `${frameHeight * scale}px`;
  }
  async function readyDocument() {
    const doc = $("report-frame").contentDocument;
    if (!doc?.body || !doc.querySelector(".sheet")) throw new Error("报告尚未加载完成，请稍候重试。");
    await doc.fonts.ready;
    await Promise.all([...doc.images].map(img => img.decode().catch(() => {
      if (!img.complete || !img.naturalWidth) throw new Error("部分报告图片未加载，请检查网络后重试。");
    })));
    frameHeight = Math.ceil(Math.max(doc.body.scrollHeight, doc.documentElement.scrollHeight));
    fitFrame();
    return doc;
  }
  function imageDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("图片预览未能准备好，请重新生成。"));
      reader.readAsDataURL(blob);
    });
  }
  async function offerImage(blob, name, text, automatic, mobile, version) {
    if (!blob || blob.size < 5000) throw new Error("图片未能完整生成，请改用分段保存。");
    const dataUrl = await imageDataUrl(blob);
    if (version !== shownReport) throw new Error("记录已清除或更新，请使用当前报告。");
    const url = URL.createObjectURL(blob); blobUrls.push(url);
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = name; anchor.textContent = text;
    const item = document.createElement("div"); item.className = "generated-image";
    const actions = document.createElement("div"); actions.className = "generated-image-actions";
    const figure = document.createElement("figure"); figure.hidden = !mobile;
    const caption = document.createElement("figcaption");
    caption.textContent = window.fgoNativeGallery ? "点击保存按钮，直接存入系统相册。" : window.fgoDesktopSave ? "点击保存按钮，选择本机保存位置。" : "长按统计图片保存到手机；电脑可右键保存。";
    const preview = document.createElement("img");
    preview.alt = "统计图片，长按可保存";
    preview.src = dataUrl;
    figure.append(caption, preview);
    const open = document.createElement("button");
    open.type = "button"; open.textContent = window.fgoDesktopSave ? "查看图片" : mobile ? "查看图片 / 长按保存" : "查看图片 / 右键保存";
    open.addEventListener("click", () => {
      figure.hidden = false; figure.scrollIntoView({behavior:"smooth", block:"start"});
    });
    actions.append(anchor, open); item.append(actions, figure); $("image-links").append(item);
    $("local-images").hidden = false;
    if (window.fgoDesktopSave) {
      anchor.removeAttribute("href");
      anchor.textContent = text.replace("下载", "保存");
      anchor.addEventListener("click", async () => {
        try { await window.fgoDesktopSave(dataUrl, name, version); }
        catch (error) { $("image-progress").textContent = error.message; }
      });
      if (automatic) await window.fgoDesktopSave(dataUrl, name, version);
    } else if (automatic) anchor.click();
  }
  async function saveImages(segmented) {
    if (imageBusy || !shownReport) return;
    imageBusy = true; controls(); discardImages();
    const version = shownReport;
    const mobile = mobileSaving() && !window.fgoDesktopSave;
    const attempt = [...crypto.getRandomValues(new Uint8Array(12))].map(v => v.toString(16).padStart(2,"0")).join("");
    $("image-progress").textContent = "正在本机准备图片，首次加载素材可能稍慢…";
    try {
      const doc = await readyDocument();
      const height = frameHeight;
      const partHeight = segmented ? 3600 : height;
      const count = Math.ceil(height / partHeight);
      const coarse = window.matchMedia("(pointer: coarse)").matches;
      const pixelBudget = coarse ? 14_000_000 : 50_000_000;
      const ratio = Math.min(1.5, 28000 / Math.max(1200, partHeight), Math.sqrt(pixelBudget / (1200 * partHeight)));
      if (!segmented && ratio < .7) throw new Error("这份报告很长，建议使用“分段保存”，保留清晰度。");
      const fontEmbedCSS = await window.htmlToImage.getFontEmbedCSS(doc.body);
      for (let i = 0; i < count; i++) {
        if (version !== shownReport) throw new Error("记录已清除或更新，请使用当前报告。");
        $("image-progress").textContent = segmented ? `浏览器正在合成第 ${i + 1} / ${count} 段…` : "浏览器正在合成完整长图…";
        await new Promise(resolve => window.setTimeout(resolve, 30));
        const offset = i * partHeight;
        const clipped = Math.min(partHeight, height - offset);
        const canvas = await window.htmlToImage.toCanvas(doc.body, {
          width:1200, height:clipped, pixelRatio:ratio, backgroundColor:"#e4f3f7", fontEmbedCSS,
          style:{height:`${height}px`, margin:"0", transform:`translateY(-${offset}px)`, transformOrigin:"top left"}
        });
        const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", .94));
        canvas.width = canvas.height = 1;
        if (version !== shownReport) throw new Error("记录已清除或更新，请使用当前报告。");
        const prefix = current.preview?.region === "JP" ? "fgo-jp" : `fgo-cn-${current.server}`;
        const name = `${prefix}-report${segmented ? "-" + String(i + 1).padStart(2, "0") : ""}.jpg`;
        await offerImage(blob, name, segmented ? `下载第 ${i + 1} 段` : "下载完整长图", !segmented && !mobile, mobile, version);
      }
      $("image-progress").textContent = window.fgoNativeGallery ? (segmented ? `已生成 ${count} 段，点击下方各段保存按钮即可存入相册。` : "统计图片已保存到系统相册的“FGO抓包统计”中。") : mobile ? (segmented ? `已在本机生成 ${count} 段，请长按下方每段图片保存。` : "长图已在本机生成。请长按下方统计图片，选择“保存图片”或“保存到手机”。") : segmented ? `已在本机生成 ${count} 段，请在下方逐段保存。` : "长图已在本机生成，可在下方重新保存。";
      if (mobile) $("local-images").scrollIntoView({behavior:"smooth", block:"start"});
    } catch (error) {
      $("image-progress").textContent = error.message && !error.message.includes("fetch") ? error.message : "图片生成失败，请检查素材加载情况，或改用分段保存。";
    } finally { imageBusy = false; controls(); }
  }
  $("tab-qr").addEventListener("click", () => setMode("qr"));
  $("tab-file").addEventListener("click", () => setMode("file"));
  $("consent").addEventListener("change", controls);
  document.querySelectorAll('input[name="server"]').forEach(input => input.addEventListener("change", changeServer));
  $("capture-file").addEventListener("change", event => chooseFile(event.target.files[0]));
  $("file-drop").addEventListener("dragover", event => { event.preventDefault(); $("file-drop").classList.add("drag-over"); });
  $("file-drop").addEventListener("dragleave", () => $("file-drop").classList.remove("drag-over"));
  $("file-drop").addEventListener("drop", event => { event.preventDefault(); $("file-drop").classList.remove("drag-over"); chooseFile(event.dataTransfer.files[0]); });
  $("start-button").addEventListener("click", start);
  $("build-report").addEventListener("click", build);
  $("clear-session").addEventListener("click", async () => {
    if (pending) return;
    pending = true; sessionEpoch++; controls();
    try { render(await post("/api/clear", {})); selectedFile = null; $("capture-file").value = ""; controls(); }
    catch (error) { showError(error.code); }
    finally { pending = false; controls(); }
  });
  $("report-frame").addEventListener("load", async () => {
    if (!shownReport) return;
    try { await readyDocument(); } catch (error) { $("image-progress").textContent = error.message; }
  });
  $("download-image").addEventListener("click", () => saveImages(false));
  $("download-sections").addEventListener("click", () => saveImages(true));
  window.addEventListener("resize", fitFrame);
  window.setInterval(updateCountdown, 1000);
  controls(); poll();
})();
