/* Air Datepicker 3.6.0 中文语言包
 *
 * 官方 npm 包里的 locale/zh.js 是 ES 模块（exports.default），浏览器用
 * <script> 引不了；这里按同样的内容改成浏览器全局对象，内容与官方一致。
 *
 * 来源：https://cdn.jsdelivr.net/npm/air-datepicker@3.6.0/locale/zh.js
 */
(function () {
  "use strict";
  window.NGA_DATE_LOCALE = {
    days: ["周日", "周一", "周二", "周三", "周四", "周五", "周六"],
    daysShort: ["日", "一", "二", "三", "四", "五", "六"],
    daysMin: ["日", "一", "二", "三", "四", "五", "六"],
    months: [
      "一月", "二月", "三月", "四月", "五月", "六月",
      "七月", "八月", "九月", "十月", "十一月", "十二月",
    ],
    monthsShort: [
      "一月", "二月", "三月", "四月", "五月", "六月",
      "七月", "八月", "九月", "十月", "十一月", "十二月",
    ],
    today: "今天",
    clear: "清除",
    dateFormat: "yyyy-MM-dd",
    timeFormat: "HH:mm",
    firstDay: 1,
  };
})();
