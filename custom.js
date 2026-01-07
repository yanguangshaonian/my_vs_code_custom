// let editor_instance = null;
// // const config = { attributes: true, childList: true, subtree: true, characterDataOldValue: true, characterData: true };
// const config = { childList: true, subtree: true };
// const TARGET_SELECTOR = 'span[class*="dyn-rule-"]';


// const simple_map = {
//     "int": ":&nbsp;i32",
//     "int32_t": "i32",
//     "long": "i64",
//     "vector": "vec",
// };

// const complex_rules = [
//     // {
//     //     regex: /vector[\s\u00A0]*</g,
//     //     to: "vec<i32>"
//     // }
// ];

// const NORMALIZE_REGEX = /[\s\u00A0]+/g;

// function processTargetElement(element) {
//     const firstChild = element.firstChild;
//     if (!firstChild || firstChild.nodeType !== Node.TEXT_NODE) return;

//     let currentValue = firstChild.nodeValue;
//     // 原始值备份，用于最后对比是否发生了变化
//     const originalValue = currentValue;

//     const lookupKey = currentValue.replace(/[\s\u00A0]+/g, ' ').trim();
//     if (simple_map[lookupKey]) {
//         currentValue = simple_map[lookupKey];
//     } else {
//         for (let i = 0; i < complex_rules.length; i++) {
//             const rule = complex_rules[i];
//             // 只有当正则匹配成功时才替换
//             if (rule.regex.test(currentValue)) {
//                 currentValue = currentValue.replace(rule.regex, rule.to);
//             }
//         }
//     }

//     // --- 提交修改 ---
//     if (currentValue !== originalValue) {
//         console.log(`[替换] "${originalValue}" -> "${currentValue}"`);
//         firstChild.nodeValue = currentValue;
//     }
// }

// function editor_instance_callback(mutationsList, observer) {
//     // 使用普通的 for 循环，比 for...of 在高频触发下微乎其微地更快，且无迭代器开销
//     for (let i = 0; i < mutationsList.length; i++) {
//         const mutation = mutationsList[i];
//         if (mutation.type !== "childList") {
//             continue
//         };

//         const addedNodes = mutation.addedNodes;
//         const len = addedNodes.length;

//         if (len === 0) {
//             continue;
//         }

//         for (let j = 0; j < len; j++) {
//             const node = addedNodes[j];
//             if (node.nodeType !== Node.ELEMENT_NODE) {
//                 continue;
//             }

//             if (node.matches(TARGET_SELECTOR)) {
//                 processTargetElement(node);
//             } else if (node.firstElementChild) {
//                 const targets = node.querySelectorAll(TARGET_SELECTOR);
//                 const tLen = targets.length;
//                 for (let k = 0; k < tLen; k++) {
//                     processTargetElement(targets[k]);
//                 }
//             }
//         }
//     }
// }

// function set_editor_instance() {
//     try {
//         const observer = new MutationObserver((mutationsList, observer) => {
//             try {
//                 editor_instance_callback(mutationsList, observer)
//             } catch (e) {
//                 console.log("editor_instance_callback error: ", e)
//             }
//         });
//         observer.observe(editor_instance, config);
//     } catch (e) {
//         console.log("observer配置出错: ", e)
//     }
// }

// function find_editor_instance() {
//     console.log("find_editor_instance")
//     editor_instance = document.querySelector("div.editor-instance");
// }

// function poll() {
//     if (editor_instance) {
//         console.log("div.editor-instance 已经找到")
//         set_editor_instance();
//     } else {
//         find_editor_instance();
//         setTimeout(poll, 1000);
//     }
// }
// setTimeout(poll, 5000);;

// console.log("start custom.js!")




// (function () {
//     // 1. 备份原生的 WebSocket
//     const OriginalWebSocket = window.WebSocket;
//     const OriginalSend = OriginalWebSocket.prototype.send;

//     // 2. 你的类型映射表
//     const typeReplacements = [
//         { from: "int32_t", to: "i32" },
//         { from: "uint32_t", to: "u32" },
//         { from: "int64_t", to: "i64" },
//         { from: "vector", to: "vec" },
//         // 正则处理复杂类型，例如去除命名空间 std::
//         { regex: /std::/g, to: "" }
//     ];

//     function transformLabel(originalLabel) {
//         if (!originalLabel) return originalLabel;

//         let newLabel = originalLabel;
//         // 执行所有替换逻辑
//         typeReplacements.forEach(rule => {
//             if (rule.regex) {
//                 newLabel = newLabel.replace(rule.regex, rule.to);
//             } else {
//                 // 简单的字符串全局替换
//                 newLabel = newLabel.split(rule.from).join(rule.to);
//             }
//         });
//         return newLabel;
//     }

//     // 3. 劫持 WebSocket 构造函数
//     window.WebSocket = function (url, protocols) {
//         console.log("2233~~~~~~~~~~");
//         const ws = new OriginalWebSocket(url, protocols);

//         // 监听消息接收 (Server -> Client)
//         ws.addEventListener('message', function (event) {
//             console.log("2233~~~~~~~~");
//             // try {
//             //     // LSP 消息通常是 JSON 字符串
//             //     const data = JSON.parse(event.data);

//             //     // 检查是否是 InlayHint 的响应
//             //     // 依据：这是一个响应(result存在) 且 result 是数组 且 数组里有 inlayHint 结构
//             //     if (data.result && Array.isArray(data.result) && data.result.length > 0) {
//             //         // 简单的特征嗅探：查看第一个元素是否有 label 和 position
//             //         // 注意：clangd 的 inlayHint 可能返回 string label 或 list label
//             //         if (data.result[0].position && data.result[0].kind !== undefined) {

//             //             // --- 开始篡改 LSP 数据 ---
//             //             let modifiedCount = 0;
//             //             data.result.forEach(hint => {
//             //                 if (typeof hint.label === 'string') {
//             //                     const old = hint.label;
//             //                     hint.label = transformLabel(hint.label);
//             //                     if (old !== hint.label) modifiedCount++;
//             //                 }
//             //                 // 有些 LSP 返回的 label 是数组 parts
//             //                 else if (Array.isArray(hint.label)) {
//             //                     hint.label.forEach(part => {
//             //                         if (part.value) {
//             //                             part.value = transformLabel(part.value);
//             //                         }
//             //                     });
//             //                 }
//             //             });

//             //             if (modifiedCount > 0) {
//             //                 console.log(`[LSP Hook] 优化了 ${modifiedCount} 个类型注释`);
//             //                 // 重新打包回 JSON
//             //                 // 这一步非常关键：必须修改 event.data
//             //                 // 但 event.data 是只读的，我们需要用 Object.defineProperty 覆盖它
//             //                 const newData = JSON.stringify(data);
//             //                 Object.defineProperty(event, 'data', {
//             //                     value: newData,
//             //                     writable: false
//             //                 });
//             //             }
//             //             // -----------------------
//             //         }
//             //     }
//             // } catch (e) {
//             //     // 解析失败或不是 JSON，忽略
//             // }
//         });

//         return ws;
//     };

//     // 还原原型链 (让外部看起来还是 WebSocket)
//     window.WebSocket.prototype = OriginalWebSocket.prototype;
//     window.WebSocket.CONNECTING = OriginalWebSocket.CONNECTING;
//     window.WebSocket.OPEN = OriginalWebSocket.OPEN;
//     window.WebSocket.CLOSING = OriginalWebSocket.CLOSING;
//     window.WebSocket.CLOSED = OriginalWebSocket.CLOSED;

//     console.log("LSP WebSocket Hook 已激活: 类型注释将自动缩短");
// })();








// var CURSOR;
// Math.lerp = (a, b, n) => (1 - n) * a + n * b;

// const getStyle = (el, attr) => {
//     try {
//         return window.getComputedStyle
//             ? window.getComputedStyle(el)[attr]
//             : el.currentStyle[attr];
//     } catch (e) {}
//     return "";
// };

// class Cursor {
//     constructor() {
//         this.pos = {curr: null, prev: null};
//         this.pt = [];
//         this.create();
//         this.init();
//         this.render();
//         console.log("create cursor")
//     }

//     move(left, top) {
//         this.cursor.style["left"] = `${left}px`;
//         this.cursor.style["top"] = `${top}px`;
//     }

//     create() {
//         if (!this.cursor) {
//             this.cursor = document.createElement("div");
//             this.cursor.id = "cursor";
//             this.cursor.classList.add("hidden");
//             document.body.append(this.cursor);
//         }

//         var el = document.getElementsByTagName('*');
//         for (let i = 0; i < el.length; i++)
//             if (getStyle(el[i], "cursor") == "pointer")
//                 this.pt.push(el[i].outerHTML);

//         document.body.appendChild((this.scr = document.createElement("style")));
//         this.scr.innerHTML += `.split-view-container * {cursor: url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 8' width='8px' height='8px'><circle cx='4' cy='4' r='4' opacity='.5'/></svg>") 4 4, auto !important;}`;
//         // this.scr.innerHTML += `* {cursor: url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 8' width='8px' height='8px'><circle cx='4' cy='4' r='4' opacity='.5'/></svg>") 4 4, auto !important;}`;

//     }

//     refresh() {
//         this.scr.remove();
//         this.cursor.classList.remove("hover");
//         this.cursor.classList.remove("active");
//         this.pos = {curr: null, prev: null};
//         this.pt = [];

//         this.create();
//         this.init();
//         this.render();
//     }

//     init() {
//         document.onmouseover  = e => this.pt.includes(e.target.outerHTML) && this.cursor.classList.add("hover");
//         document.onmouseout   = e => this.pt.includes(e.target.outerHTML) && this.cursor.classList.remove("hover");
//         document.onmousemove  = e => {(this.pos.curr == null) && this.move(e.clientX - 8, e.clientY - 8); this.pos.curr = {x: e.clientX - 8, y: e.clientY - 8}; this.cursor.classList.remove("hidden");};
//         document.onmouseenter = e => this.cursor.classList.remove("hidden");
//         document.onmouseleave = e => this.cursor.classList.add("hidden");
//         document.onmousedown  = e => this.cursor.classList.add("active");
//         document.onmouseup    = e => this.cursor.classList.remove("active");
//     }

//     render() {
//         if (this.pos.prev) {
//             this.pos.prev.x = Math.lerp(this.pos.prev.x, this.pos.curr.x, 0.15);
//             this.pos.prev.y = Math.lerp(this.pos.prev.y, this.pos.curr.y, 0.15);
//             this.move(this.pos.prev.x, this.pos.prev.y);
//         } else {
//             this.pos.prev = this.pos.curr;
//         }
//         requestAnimationFrame(() => this.render());
//     }
// }

// (() => {
//     CURSOR = new Cursor();
//     // 需要重新获取列表时，使用 CURSOR.refresh()
// })();








// console.log("start custom.js")

// var CURSOR;

// Math.lerp = (a, b, n) => (1 - n) * a + n * b;

// const getStyle = (el, attr) => {
//     try {
//         return window.getComputedStyle
//             ? window.getComputedStyle(el)[attr]
//             : el.currentStyle[attr];
//     } catch (e) {}
//     return "";
// };

// // 工具函数：节流 + 防抖
// function throttleDebounce(fn, limit = 50) {  // 默认 50ms ≈ 20fps
//     let inThrottle = false;
//     let lastArgs = null;
//     return function(...args) {
//         if (!inThrottle) {
//             fn.apply(this, args);   // 立即执行一次
//             inThrottle = true;
//             setTimeout(() => {
//                 inThrottle = false;
//                 if (lastArgs) {
//                     fn.apply(this, lastArgs); // 防抖：执行最后一次
//                     lastArgs = null;
//                 }
//             }, limit);
//         } else {
//             lastArgs = args; // 记录最后一次参数
//         }
//     };
// }

// class Cursor {
//     constructor() {
//         this.pos = {curr: null, prev: null};
//         this.pt = new Set();   // 用 Set 存节点
//         this.create();
//         this.init();
//         this.render();
//         console.log("create cursor")
//     }

//     move(left, top) {
//         this.cursor.style.left = `${left}px`;
//         this.cursor.style.top = `${top}px`;
//         // this.cursor.style.transform = `translate3d(${left}px, ${top}px, 0)`;
//     }

//     create() {
//         if (!this.cursor) {
//             this.cursor = document.createElement("div");
//             this.cursor.id = "cursor";
//             this.cursor.classList.add("hidden");
//             document.body.append(this.cursor);
//         }

//         // 找出 cursor=pointer 的元素，存 DOM 引用而不是 outerHTML
//         const el = document.getElementsByTagName('*');
//         for (let i = 0; i < el.length; i++) {
//             if (getStyle(el[i], "cursor") === "pointer") {
//                 this.pt.add(el[i]);
//             }
//         }

//         // 注入自定义样式
//         this.scr = document.createElement("style");
//         this.scr.textContent = `
//             .split-view-container * {
//                 cursor: url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 8' width='8px' height='8px'><circle cx='4' cy='4' r='4' opacity='.5'/></svg>") 4 4, auto !important;
//             }
//         `;
//         document.body.appendChild(this.scr);
//     }

//     refresh() {
//         this.scr.remove();
//         this.cursor.classList.remove("hover", "active");
//         this.pos = {curr: null, prev: null};
//         this.pt.clear();  // 清空 Set

//         this.create();
//         this.init();
//         this.render();
//     }

//     init() {
//         document.onmouseover  = e => this.pt.has(e.target) && this.cursor.classList.add("hover");
//         document.onmouseout   = e => this.pt.has(e.target) && this.cursor.classList.remove("hover");

//         // 使用节流 + 防抖包装 mousemove
//         document.onmousemove  = throttleDebounce(e => {
//             if (this.pos.curr == null) this.move(e.clientX - 8, e.clientY - 8);
//             this.pos.curr = {x: e.clientX - 8, y: e.clientY - 8};
//             this.cursor.classList.remove("hidden");
//         }, 20); // ~50fps

//         document.onmouseenter = () => this.cursor.classList.remove("hidden");
//         document.onmouseleave = () => this.cursor.classList.add("hidden");
//         document.onmousedown  = () => this.cursor.classList.add("active");
//         document.onmouseup    = () => this.cursor.classList.remove("active");
//     }

//     render() {
//         if (this.pos.prev) {
//             this.pos.prev.x = Math.lerp(this.pos.prev.x, this.pos.curr.x, 0.15);
//             this.pos.prev.y = Math.lerp(this.pos.prev.y, this.pos.curr.y, 0.15);
//             this.move(this.pos.prev.x, this.pos.prev.y);
//         } else {
//             this.pos.prev = this.pos.curr;
//         }
//         requestAnimationFrame(() => this.render());
//     }
// }

// (() => {
//     CURSOR = new Cursor();
//     // 需要重新获取列表时，使用 CURSOR.refresh()
// })();