"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const csrf = document.querySelector('meta[name="fgo-csrf"]').content;
  const types = {stone:"圣晶石", friend:"友情点", lucky:"福袋", ticket:"兑换券", unknown:"未识别类型"};
  let generation = -1, engine = "", opened = false, page = 1, pages = 1, serial = 0;
  let request = null, timer = null, images = [], imageRequests = [];
  let region = "CN";
  const count = value => new Intl.NumberFormat("zh-CN").format(value);
  const date = value => value ? new Intl.DateTimeFormat("zh-CN", {
    timeZone:region === "JP" ? "Asia/Tokyo" : "Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit",
    hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false
  }).format(new Date(value * 1000)) : "未提供获取时间";
  const day = value => value ? date(value).split(" ")[0] : "时间未提供";
  const text = (tag, value, className = "") => {
    const element = document.createElement(tag);
    element.textContent = value; element.className = className;
    return element;
  };
  function clearRows() {
    imageRequests.forEach(controller => controller.abort()); imageRequests = [];
    images.forEach(url => URL.revokeObjectURL(url)); images = [];
    $("detail-list").replaceChildren();
  }
  function reset() {
    serial++;
    request?.abort(); request = null;
    window.clearTimeout(timer);
    clearRows(); opened = false; page = pages = 1;
    $("detail-browser").hidden = true;
    $("detail-query").value = "";
    $("detail-status").textContent = "";
    $("detail-pagination").hidden = true;
    $("show-gachas").setAttribute("aria-pressed", "false");
  }
  window.addEventListener("fgo-state", event => {
    const state = event.detail;
    if (!state.ready || state.generation !== generation || state.engine !== engine) reset();
    generation = state.generation; engine = state.engine;
    $("account-detail-card").hidden = !state.ready;
  });
  async function loadImage(record, container, note, button) {
    const version = generation;
    const controller = new AbortController(); imageRequests.push(controller);
    note.textContent = "正在加载卡池图片…"; button.disabled = true;
    try {
      const response = await fetch(`/api/gachas/${record.gacha_id}/image?generation=${version}`, {
        credentials:"same-origin", cache:"no-store", signal:controller.signal,
        headers:{"X-FGO-CSRF":csrf}
      });
      if (!response.ok) throw new Error("IMAGE_UNAVAILABLE");
      const blob = await response.blob();
      if (version !== generation || !container.isConnected) return;
      const url = URL.createObjectURL(blob); images.push(url);
      const image = document.createElement("img");
      image.alt = record.name || `卡池 #${record.gacha_id}`;
      image.src = url; image.decoding = "async";
      container.prepend(image); note.textContent = "部分历史卡池图片可能复用，请以卡池名称、ID 和抽数为准。";
      button.hidden = true;
    } catch (error) {
      if (error.name === "AbortError") return;
      note.textContent = "图片暂不可用，抽数仍可正常查看。";
      button.textContent = "重新加载图片"; button.disabled = false;
    } finally {
      imageRequests = imageRequests.filter(item => item !== controller);
    }
  }
  function gachaRow(record) {
    const row = document.createElement("details"); row.className = "detail-row gacha-detail";
    const summary = document.createElement("summary");
    const title = text("div", "", "detail-row-title");
    title.append(text("h3", record.name || `未知卡池 #${record.gacha_id}`));
    title.append(text("p", `${types[record.kind]} · ID ${record.gacha_id} · ${day(record.opened_at)}`));
    const total = text("strong", `${count(record.pulls)} 抽`, "detail-number");
    summary.append(title, total); row.append(summary);
    const body = document.createElement("div"); body.className = "detail-row-body";
    body.append(text("p", `${day(record.opened_at)} — ${day(record.closed_at)}`, "detail-period"));
    const note = text("p", record.image_id ? "展开后按需加载图片。" : "该卡池暂无对应图片。", "detail-image-note");
    const button = text("button", "加载卡池图片", "button light"); button.type = "button";
    body.append(note, button); button.hidden = !record.image_id;
    let started = false;
    const load = () => { started = true; loadImage(record, body, note, button); };
    button.addEventListener("click", load);
    row.addEventListener("toggle", () => { if (row.open && record.image_id && !started) load(); });
    row.append(body);
    return row;
  }
  async function load() {
    request?.abort(); request = new AbortController();
    const current = ++serial, version = generation;
    clearRows();
    $("detail-status").textContent = "正在读取卡池记录…";
    $("detail-pagination").hidden = true;
    const query = new URLSearchParams({q:$("detail-query").value, kind:$("detail-kind").value, sort:$("detail-sort").value, page:String(page)});
    try {
      const response = await fetch(`/api/gachas?${query}`, {
        credentials:"same-origin", cache:"no-store", signal:request.signal, headers:{"X-FGO-CSRF":csrf}
      });
      const data = await response.json();
      if (!response.ok) throw Object.assign(new Error(), {code:data.error});
      if (current !== serial || version !== generation || data.generation !== generation) return;
      page = data.page; pages = data.pages;
      region = data.region === "JP" ? "JP" : "CN";
      $("detail-status").textContent = !data.available ? "文件缺少卡池记录数据表。" : `${region === "JP" ? "日服" : "国服"} · ${count(data.total)} 个卡池 · 累计 ${count(data.summary.pulls)} 抽`;
      for (const record of data.rows) $("detail-list").append(gachaRow(record));
      if (!data.rows.length && data.available) $("detail-list").append(text("p", "没有符合条件的记录。", "detail-empty"));
      $("detail-pagination").hidden = pages <= 1;
      $("detail-page").textContent = `${page} / ${pages}`;
      $("detail-prev").disabled = page <= 1; $("detail-next").disabled = page >= pages;
    } catch (error) {
      if (error.name === "AbortError" || current !== serial) return;
      $("detail-status").textContent = ["STATISTICS_NOT_READY", "VISITOR_EXPIRED"].includes(error.code) ? "统计已过期，请重新扫码或导入文件。" : error.code === "REPORT_NOT_CONFIGURED" ? "请先准备统计字典，再查看明细。" : "明细读取失败，请点击上方按钮重试。";
    }
  }
  function show() {
    opened = true; page = 1; $("detail-browser").hidden = false;
    $("detail-query").value = "";
    $("show-gachas").setAttribute("aria-pressed", "true");
    $("detail-scope").textContent = "日替和不同阶段的剧情池分别记录。默认友情与常驻／长期卡池置顶，其余按开放时间从新到旧排列；点击卡池才加载图片。";
    $("detail-kind").replaceChildren();
    for (const [key, label] of [["all", "全部类型"], ...Object.entries(types)]) {
      const option = text("option", label); option.value = key; $("detail-kind").append(option);
    }
    $("detail-sort").value = "default";
    load();
  }
  $("show-gachas").addEventListener("click", show);
  $("detail-query").addEventListener("input", () => {
    if (!opened) return;
    window.clearTimeout(timer); page = 1; request?.abort(); serial++;
    timer = window.setTimeout(load, 250);
  });
  for (const id of ["detail-kind", "detail-sort"]) $(id).addEventListener("change", () => { page = 1; load(); });
  $("detail-prev").addEventListener("click", () => { if (page > 1) { page--; load(); } });
  $("detail-next").addEventListener("click", () => { if (page < pages) { page++; load(); } });
})();
