/* 安卓消息桥只接受本机主页面，保存通过系统文件选择器完成。 */
(() => {
  "use strict";
  const $ = id => document.getElementById(id), pending = new Map();
  let sequence = 0, busy = false;
  window.fgoNativeGallery = true;
  const navigation = document.createElement("nav");
  navigation.className = "mobile-navigation";
  navigation.setAttribute("aria-label", "页面导航");
  for (const [label, target] of [["获取数据", "entry-title"], ["统计图", "report-title"], ["资源", "local-setup-title"]]) {
    const button = document.createElement("button");
    button.type = "button"; button.textContent = label;
    button.addEventListener("click", () => $(target).scrollIntoView({behavior:"auto", block:"start"}));
    navigation.append(button);
  }
  document.body.append(navigation);
  window.FgoNative.onmessage = event => {
    const value = JSON.parse(event.data), task = pending.get(value.id);
    if (!task) return;
    pending.delete(value.id);
    const messages = {
      PHOTO_PERMISSION_DENIED:"未获得保存相册所需的存储权限，请再次保存并授权。",
      PHOTO_SAVE_FAILED:"图片未能保存到相册，请检查可用空间和相册权限。",
      SAVE_EXPIRED:"本次图片已过期，请重新生成后保存。",
      BROWSER_UNAVAILABLE:"无法打开系统浏览器，请先安装或启用浏览器。"
    };
    value.ok ? task.resolve(value) : task.reject(new Error(messages[value.error] || value.message || `操作未完成（${value.error}）`));
  };
  function command(method, args = []) {
    return new Promise((resolve, reject) => {
      const id = String(++sequence);
      pending.set(id, {resolve, reject});
      window.FgoNative.postMessage(JSON.stringify({id, method, args}));
    });
  }
  function notice(text) { $("desktop-notice").textContent = text; }
  $("source-repository").addEventListener("click", async event => {
    event.preventDefault();
    try { await command("open_repository"); }
    catch (error) { notice(error.message); }
  });
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const value = await command("status");
      window.renderResourceStatus(value.resources, value.dictionary_directory);
    } catch (error) { notice(error.message); }
    finally { busy = false; }
  }
  $("desktop-existing").hidden = $("desktop-open-dictionary").hidden = $("desktop-open-folder").hidden = true;
  $("desktop-prepare").addEventListener("click", async () => {
    notice("已开始查询上游资源版本，进度和结果会显示在下方。");
    try { await command("prepare_resources"); await refresh(); }
    catch (error) { notice(error.message); }
  });
  $("download-php").addEventListener("click", async event => {
    event.preventDefault();
    try {
      const result = await command("save_php");
      notice(result.saved ? "原始 PHP 数据已保存到所选位置。" : "已取消保存。");
    } catch (error) { notice(error.message); }
  });
  window.fgoDesktopSave = async (dataUrl, name, version) => {
    const result = await command("save_image", [dataUrl, name, version]);
    notice(result.saved ? "统计图片已保存到系统相册的“FGO抓包统计”中。" : "图片尚未保存，仍可在本页查看。");
  };
  command("connect", [document.querySelector('meta[name="fgo-csrf"]').content])
    .then(() => { refresh(); window.setInterval(refresh, 1200); })
    .catch(error => notice(error.message));
})();
