"use strict";
// 所有账号、挑战与结果均为人工值，不请求外网。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));
const response = value => ({ok:true, json:async () => value});

async function page(holdChallenge = false, origin = "http://127.0.0.1:55313") {
  let state = {generation:1, source:"", server:"ios", phase:"ready", password_available:true,
    qr_available:true, has_qr:false, captcha_frame_url:"/captcha-frame.html"};
  const elements = new Map(), requests = [], listeners = new Map();
  let iframe, resolveChallenge;
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value:"", checked:id === "consent", disabled:false, hidden:false, open:false, events:{}, style:{},
      classList:{toggle(){}, add(){}, remove(){}},
      addEventListener(name, cb) { this.events[name] = cb; },
      setAttribute(name, value) { this[name] = value; },
      removeAttribute(name) { delete this[name]; },
      replaceChildren() {},
      appendChild(child) { iframe = child; },
      querySelector(selector) { return element(id + selector); },
      showModal() { this.open = true; }, close() { this.open = false; }, focus() {}
    });
    return elements.get(id);
  };
  const radios = ["ios", "android"].map(value => ({value, checked:value === "ios", addEventListener(){}}));
  const add = (name, cb) => listeners.set(name, cb);
  const remove = (name, cb) => { if (listeners.get(name) === cb) listeners.delete(name); };
  const context = {
    document:{getElementById:element, querySelector:selector => selector.startsWith("meta")
      ? {content:"synthetic-csrf"} : radios.find(input => input.checked), querySelectorAll:() => radios,
      createElement:() => ({attributes:{}, setAttribute(name,value){this.attributes[name]=value;},
        contentWindow:{postMessage(data){iframe.startMessage=data;}}, remove(){this.removed=true;}})},
    window:{addEventListener:add, dispatchEvent(){}, setInterval(){}, setTimeout(){}},
    addEventListener:add, removeEventListener:remove, clearTimeout(){}, setTimeout(){return 1;},
    performance:{now:()=>0}, CustomEvent:class {}, URL,
    location:{href:origin + "/"}, crypto:{getRandomValues(bytes){bytes.fill(7);return bytes;}},
    fetch:async (path, options) => {
      requests.push({path, options});
      if (path === "/api/state") return response(state);
      if (path === "/api/password") {
        state={...state, phase:"password_verification", generation:2, source:"password"};
        return response(state);
      }
      if (path === "/api/password/challenge") {
        if (holdChallenge) return new Promise(resolve => { resolveChallenge=resolve; });
        return response({challenge_id:"synthetic-id", gt:"synthetic-gt", challenge:"synthetic-challenge"});
      }
      if (path === "/api/password/verify") {
        state={...state, phase:"password_authenticating"}; return response(state);
      }
      if (path === "/api/password/cancel") {
        state={...state, phase:"cleared", generation:3}; return response(state);
      }
      throw new Error("UNEXPECTED_REQUEST");
    }
  };
  vm.runInNewContext(source + "\n;globalThis.__captchaIsolation = FgoCaptchaIsolation;", context);
  await settle();
  element("tab-password").events.click();
  element("password-account").value="synthetic-user";
  element("password-secret").value="synthetic-password";
  element("password-secret").events.input();
  assert.equal(element("start-button").disabled, false);
  await element("start-button").events.click();
  await settle();
  assert.equal(element("password-secret").value, "");
  return {element, requests, listeners, context, get iframe(){return iframe;}, resolveChallenge};
}
(async () => {
  const view=await page();
  assert.equal(view.element("password-verification").open,true);
  assert.equal(view.iframe.attributes.sandbox,"allow-scripts");
  assert.equal(view.iframe.src,"http://127.0.0.1:55313/captcha-frame.html");
  const receive=view.listeners.get("message");
  receive({source:{},origin:"null",data:{status:"hello"}});
  assert.equal(view.iframe.startMessage,undefined);
  receive({source:view.iframe.contentWindow,origin:"http://127.0.0.1:55313",data:{status:"hello"}});
  assert.equal(view.iframe.startMessage,undefined);
  receive({source:view.iframe.contentWindow,origin:"null",data:{status:"hello"}});
  assert.equal(view.iframe.startMessage.type,"start");
  receive({source:view.iframe.contentWindow,origin:"null",data:{status:"success",nonce:"wrong",result:{challenge:"c",validate:"v",seccode:"s"}}});
  assert.equal(view.requests.filter(x=>x.path==="/api/password/verify").length,0);
  receive({source:view.iframe.contentWindow,origin:"null",data:{status:"success",nonce:view.iframe.startMessage.nonce,result:{challenge:"c",validate:"v",seccode:"s"}}});
  await settle();
  const verify=view.requests.find(x=>x.path==="/api/password/verify");
  assert.deepEqual(JSON.parse(verify.options.body),{generation:2,challenge_id:"synthetic-id",result:{challenge:"c",validate:"v",seccode:"s"}});
  assert.equal(view.element("password-verification").open,false);
  const cancel=await page(true);
  await cancel.element("cancel-verification").events.click();
  assert.equal(cancel.element("password-verification").open,false);
  cancel.resolveChallenge(response({challenge_id:"late",gt:"g",challenge:"c"}));
  await settle();
  assert.equal(cancel.iframe,undefined);
  assert.equal(cancel.requests.filter(x=>x.path==="/api/password/verify").length,0);
  const ipv6=await page(false,"http://[::1]:55313");
  assert.equal(ipv6.iframe.src,"http://[::1]:55313/captcha-frame.html");
  const isolation=ipv6.context.__captchaIsolation;
  const challenge={gt:"synthetic-gt",challenge:"synthetic-challenge"};
  for (const url of ["http://[::1]:55314/captcha-frame.html", "http://127.0.0.1:55313/captcha-frame.html", "http://localhost.example:55313/captcha-frame.html"]) {
    assert.throws(()=>isolation.start(ipv6.element("password-captcha"),challenge,url), /CAPTCHA_INPUT_INVALID/);
  }
  ipv6.context.location.href="http://localhost.example:55313/";
  assert.throws(()=>isolation.start(ipv6.element("password-captcha"),challenge,"/captcha-frame.html"), /CAPTCHA_INPUT_INVALID/);
  console.log("PASSWORD_UI_SYNTHETIC_OK");
})().catch(error => {console.error(error);process.exitCode=1;});
