/* 长表「按名称定位」：把下拉选中的股票滚到滚动容器可见区域。
 *
 * 表格本身由服务端渲染（`_stock_jump.html` 生成 <select data-stock-jump>），
 * 这里只做两件事：
 *   1. 找到目标容器和 `tr[data-symbol]`；
 *   2. 用「行相对容器的偏移 − 粘性表头高度」算 scrollTop，直接跳过去。
 *
 * 不缓存行位置：浏览器缩放、列换行、窗口变宽都会改变行高，现算才不会偏。
 */
(function () {
  "use strict";

  function jump(select) {
    var id = select.getAttribute("data-stock-jump");
    var box = id && document.getElementById(id);
    if (!box) return;

    select.addEventListener("change", function () {
      var code = select.value;
      if (!code) return;
      var row = box.querySelector('tr[data-symbol="' + code + '"]');
      if (!row) return;

      var head = box.querySelector("thead");
      var sticky = head ? head.getBoundingClientRect().height : 0;
      var boxRect = box.getBoundingClientRect();
      var rowRect = row.getBoundingClientRect();
      var top = box.scrollTop + (rowRect.top - boxRect.top) - sticky - 4;

      /* 直接跳，不做平滑动画：几百行时 smooth 要滚一两秒，反而耽误事。 */
      box.scrollTop = Math.max(0, top);

      row.classList.add("jump-hit");
      window.setTimeout(function () { row.classList.remove("jump-hit"); }, 1800);

      /* 回到占位项：手动滚走后再选同一只，change 还能再触发一次。 */
      select.value = "";
    });
  }

  function init() {
    var all = document.querySelectorAll("select[data-stock-jump]");
    for (var i = 0; i < all.length; i++) jump(all[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
