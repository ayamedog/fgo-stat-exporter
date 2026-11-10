"use strict";
// 仅供无 same-origin 权限的 sandbox iframe 使用，不能接触父页凭据。
(() => {
  const parentOrigin = document.querySelector('meta[name="captcha-parent-origin"]').content;
  let validParent = false;
  try {
    const url = new URL(parentOrigin);
    validParent = url.origin === parentOrigin && ["https:", "http:"].includes(url.protocol);
  } catch (_) { /* 没有正确配置时禁止启动。 */ }
  let nonce = "", started = false, finished = false;
  const text = value => typeof value === "string" && value.length > 0 && value.length <= 1024 &&
    /^[\x21-\x7e]+$/.test(value);
  function send(status, result) {
    if (finished || !nonce) return;
    if (status === "success" || status === "error") finished = true;
    parent.postMessage(result ? {status, nonce, result} : {status, nonce}, parentOrigin);
  }
  addEventListener("message", event => {
    const value = event.data;
    if (!validParent || started || event.source !== parent || event.origin !== parentOrigin || !value ||
        value.type !== "start" || typeof value.nonce !== "string" ||
        !/^[a-f0-9]{64}$/.test(value.nonce) || !text(value.gt) || !text(value.challenge)) return;
    started = true;
    nonce = value.nonce;
    if (typeof window.initGeetest !== "function") { send("error"); return; }
    try {
      window.initGeetest({gt:value.gt, challenge:value.challenge, offline:false,
        new_captcha:true, product:"bind", https:true}, captcha => {
        if (finished) return;
        captcha.onReady(() => {
          if (finished) return;
          document.getElementById("captcha-message").textContent = "请完成验证。";
          try { captcha.verify(); } catch (_) { send("error"); }
        }).onSuccess(() => {
          if (finished) return;
          try {
            const result = captcha.getValidate();
            if (!result || !text(result.geetest_challenge) || !text(result.geetest_validate) ||
                !text(result.geetest_seccode)) { send("error"); return; }
            send("success", {challenge:result.geetest_challenge,
              validate:result.geetest_validate, seccode:result.geetest_seccode});
          } catch (_) { send("error"); }
        }).onError(() => send("error"));
      });
    } catch (_) { send("error"); }
  });
  addEventListener("DOMContentLoaded", () => {
    if (validParent) parent.postMessage({status:"hello"}, parentOrigin);
  }, {once:true});
})();
