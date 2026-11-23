/* 点图筛明细：/cycle /quality /top /review 四页共用的「表格筛选」那一层。
 *
 * 四个页面点的东西不一样（散点圆点、横条、柱子、环形扇区），但点完之后要做的事
 * 完全一样：藏掉不相关的行、改标题里的计数、亮出「已筛选 + 显示全部」的提示条。
 * 抽在这里，色/文案/滚动行为就只有一份，不会四页各写一套再慢慢走偏。
 *
 * 模板要给的 id：
 *   table  长表容器（行用 `tr[data-symbol]`；分组筛选靠各页自己的 `data-*`）
 *   count  标题里的计数节点（`<span id="...">`）
 *   bar / text / clear  提示条、提示文案、「显示全部」按钮
 *   empty  空态行（`<tr hidden><td colspan=...>`）
 *
 * 用法：
 *   const f = createChartFilter({table: 'quality-table', ...});
 *   f.apply(tr => tr.dataset.symbol === code, '散点：宝钢股份（sh600019）');
 *   f.reset();          // 还原全表
 *   f.active            // 当前是否在筛
 *   onReset: fn         // 用户按「显示全部」或从下拉跳转时回调，页面用来撤掉高亮
 */
(function () {
  "use strict";

  function createChartFilter(opts) {
    var box = document.getElementById(opts.table);
    if (!box) return null;

    var rows = Array.prototype.slice.call(
      box.querySelectorAll("tbody tr[data-symbol]"));
    var total = rows.length;
    var countEl = document.getElementById(opts.count);
    var bar = document.getElementById(opts.bar);
    var textEl = document.getElementById(opts.text);
    var emptyRow = document.getElementById(opts.empty);
    var clearBtn = document.getElementById(opts.clear);

    function setCount(n) {
      if (countEl) countEl.textContent = n + " 只";
    }

    function apply(pred, text) {
      var n = 0;
      for (var i = 0; i < rows.length; i++) {
        var hit = !!pred(rows[i]);
        rows[i].hidden = !hit;
        if (hit) n += 1;
      }
      api.active = true;
      if (emptyRow) emptyRow.hidden = n > 0;
      setCount(n);
      box.scrollTop = 0;
      if (bar) {
        if (textEl) {
          textEl.textContent = text + "；命中 " + n + " / " + total
            + " 只，再点同一处或按「显示全部」取消";
        }
        bar.hidden = false;
        /* 明细在图表下面：筛完得让人看见表，不然"点了没反应"。 */
        bar.scrollIntoView({block: "nearest", behavior: "smooth"});
      }
      return n;
    }

    function reset() {
      for (var i = 0; i < rows.length; i++) rows[i].hidden = false;
      api.active = false;
      if (emptyRow) emptyRow.hidden = true;
      setCount(total);
      if (bar) bar.hidden = true;
      box.scrollTop = 0;
      return total;
    }

    function resetAll() {
      reset();
      if (typeof opts.onReset === "function") opts.onReset();
    }

    var api = {
      active: false,
      total: total,
      rows: rows,
      apply: apply,
      reset: reset,
    };

    if (clearBtn) clearBtn.addEventListener("click", resetAll);

    /* 筛选状态下又用「按名称跳转」找另一只票：得先撤掉筛选，否则目标行还是
     * 隐藏的，跳过去一片空白。挂在 document 的捕获阶段——同一元素上的监听器
     * 按注册顺序执行，谁先谁后靠不住，捕获阶段一定早于 stock-jump.js。 */
    var jumpSel = 'select[data-stock-jump="' + opts.table + '"]';
    document.addEventListener("change", function (evt) {
      var t = evt.target;
      if (api.active && t && t.matches && t.matches(jumpSel)) resetAll();
    }, true);

    return api;
  }

  window.createChartFilter = createChartFilter;
})();
