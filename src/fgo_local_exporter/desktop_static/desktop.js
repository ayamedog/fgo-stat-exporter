/* 本机窗口桥；只调用已绑定当前回环页面会话的本机方法。 */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const explanations = {
    RESOURCE_DOWNLOAD_FAILED:"公开资源下载失败，可重试；不需要连接统计网站。",
    RESOURCE_VERSION_CHANGED:"上游资源已变化，未使用未验证的版本。请更新程序或选择已核验资源。",
    RESOURCE_PREPARE_FAILED:"资源未准备完整，请重试或选择已有资源目录。",
    EXPORT_PATH_EXISTS:"所选文件已经存在，请换一个文件名保存。",
    EXPORT_EXTENSION_INVALID:"请保留文件的 PHP、JSON 或 JPG 扩展名。",
    LOCAL_OPERATION_FAILED:"操作未完成，请重试。",
    DATA_DIRECTORY_REQUIRED:"请先点击“选择数据目录”，设置字典和图片缓存的保存位置。",
    DATA_DIRECTORY_BUSY:"正在选择目录或准备资源，请稍候。",
    DATA_DIRECTORY_UNAVAILABLE:"所选目录无法写入，请选择固定磁盘上的其他文件夹。",
    DATA_SETTINGS_UNAVAILABLE:"无法记住数据目录，请检查 Windows 用户权限后重试。",
    BROWSER_UNAVAILABLE:"无法打开系统浏览器，请检查默认浏览器设置。"
  };
  let connected = false, updating = false, selectingDirectory = false;
  const storageRow = document.createElement("div");
  storageRow.className = "desktop-directory";
  storageRow.innerHTML = '<label for="desktop-data-directory">数据保存位置</label><input id="desktop-data-directory" readonly aria-label="数据保存位置" placeholder="尚未选择数据目录"><button id="desktop-change-data" class="button primary" type="button" disabled>选择数据目录</button>';
  document.querySelector(".desktop-status-title").insertAdjacentElement("afterend", storageRow);
  const storageHint = document.createElement("p");
  storageHint.className = "desktop-secondary";
  storageHint.id = "desktop-storage-hint";
  storageRow.insertAdjacentElement("afterend", storageHint);
  $("desktop-change-data").addEventListener("click", async () => {
    const button = $("desktop-change-data");
    selectingDirectory = true;
    button.disabled = true;
    try {
      const result = await command("choose_data_directory");
      if (result.changed) notice(result.restart_required ? "数据目录已设置，重启应用后生效。原目录的文件会保留。" : result.first_selection ? "数据目录已设置，资源准备和更新进度会显示在下方。" : "已继续使用当前数据目录。");
      else notice("已取消选择，当前设置未改变。可以随时重新选择目录。");
      await refresh();
    } catch (error) { notice(error.message); }
    finally { selectingDirectory = false; button.disabled = false; }
  });
  const openData = document.createElement("button");
  openData.id = "desktop-open-data";
  openData.type = "button";
  openData.className = "button light";
  openData.textContent = "打开数据目录";
  $("desktop-existing").insertAdjacentElement("afterend", openData);
  openData.addEventListener("click", async () => {
    try { await command("open_data_folder"); }
    catch (error) { notice(error.message); }
  });
  const documents = document.createElement("button");
  documents.id = "desktop-documents";
  documents.type = "button";
  documents.className = "button light";
  documents.textContent = "使用说明与许可";
  $("desktop-open-folder").insertAdjacentElement("afterend", documents);
  documents.addEventListener("click", async () => {
    try { await command("open_documents"); }
    catch (error) { notice(error.message); }
  });
  function notice(text) { $("desktop-notice").textContent = text; }
  $("source-repository").addEventListener("click", async event => {
    event.preventDefault();
    try { await command("open_repository"); }
    catch (error) { notice(error.message); }
  });
  function failed(value) {
    if (!value?.ok) throw new Error((explanations[value?.error] || "操作未完成") + ` (${value?.error || "LOCAL_OPERATION_FAILED"})`);
    return value;
  }
  async function command(name, ...args) {
    if (!connected) throw new Error("桌面窗口尚未就绪，请稍候。");
    return failed(await window.pywebview.api[name](...args));
  }
  async function refresh() {
    if (!connected || updating) return;
    updating = true;
    try {
      const value = await command("status");
      const configured = !!value.data_directory;
      let busy = false;
      if (configured) busy = window.renderResourceStatus(value.resources, value.dictionary_directory);
      else {
        $("desktop-resource-state").textContent = "未选择目录";
        $("desktop-resource-state").dataset.state = "waiting";
        $("desktop-resources").textContent = "先选择数据目录，再准备字典和图片缓存。";
        $("desktop-dictionary-meta").textContent = "字典：尚未准备";
        $("desktop-images-meta").textContent = "图片：尚未准备";
        $("desktop-dictionary-directory").value = "选择数据目录后显示字典位置";
        $("desktop-open-dictionary").disabled = true;
        $("desktop-progress-area").hidden = true;
      }
      const choosing = selectingDirectory || value.selecting_data_directory;
      $("desktop-change-data").disabled = choosing;
      $("desktop-change-data").textContent = configured ? "更换数据目录" : "选择数据目录";
      $("desktop-change-data").className = configured ? "button light" : "button primary";
      $("desktop-prepare").disabled = $("desktop-existing").disabled = !configured || busy || choosing;
      $("desktop-open-data").disabled = !configured;
      $("desktop-data-directory").value = value.data_directory;
      storageHint.textContent = !configured ? "请手动选择字典和图片缓存的保存位置。选择后即可继续使用，无需重启。" : value.pending_data_directory ? `重启后使用：${value.pending_data_directory}。原目录的文件会保留。` : "字典和图片缓存保存在此目录；下次启动也会沿用。";
      $("desktop-open-folder").hidden = !value.can_open_folder;
    } catch (error) { notice(error.message); }
    finally { updating = false; }
  }
  for (const [id, name] of [["desktop-open-dictionary","open_dictionary_folder"],["desktop-prepare","prepare_resources"],["desktop-existing","choose_resources"],["desktop-open-folder","open_folder"]]) {
    $(id).addEventListener("click", async () => {
      const button = $(id);
      button.disabled = true;
      if (name === "prepare_resources") notice("已开始查询上游资源版本，进度和结果会显示在下方。");
      try { await command(name); await refresh(); }
      catch (error) { notice(error.message); button.disabled = false; }
    });
  }
  $("download-php").addEventListener("click", async event => {
    event.preventDefault();
    try {
      const result = await command("save_php");
      notice(result.saved ? `原始 PHP 数据已保存：${result.name}` : "已取消保存，没有写入文件。");
      await refresh();
    } catch (error) { notice(error.message); }
  });
  window.fgoDesktopSave = async (dataUrl, name, version) => {
    const result = await command("save_image", dataUrl, name, version);
    notice(result.saved ? `统计图片已保存：${result.name}` : "已取消保存，没有写入文件。");
    await refresh();
  };
  async function ready() {
    try {
      const csrf = document.querySelector('meta[name="fgo-csrf"]').content;
      failed(await window.pywebview.api.connect(csrf));
      connected = true;
      await refresh();
      window.setInterval(refresh, 400);
    } catch (error) { notice(error.message); }
  }
  if (window.pywebview?.api) ready();
  else window.addEventListener("pywebviewready", ready, {once:true});
})();
