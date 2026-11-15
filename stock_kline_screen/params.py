"""规则参数。与原版（kline_screen/params.py v1.10）逐条对齐，删掉的都注明了原因。"""

# ---------------- 七条规则的可调阈值（与原版一致） ----------------
K_VOL = 1.5           # ② 启动日放量倍数
CHG_LO, CHG_HI = 0.5, 20.5    # ③ 当日涨幅区间 %
C3_RATIO = 0.95       # ④ MA20 >= MA60 × 该值（v1.4 由 0.98 放宽）
C2_TOL = 0.01         # ④ MA20 近3日跌幅容忍度
MA60_GAP = 0.0        # 条件9 close/MA60 上限；0 = 关闭
MIN_HISTORY = 70      # find_start 要求基准日 S 的索引 i ≥ 70（含当日 ≥71 根）

# ---------------- K 线深度 ----------------
KLINE_BARS = 300      # 向 hub 要的根数（hub 的滚动深度就是 300）
KLINE_MIN_BARS = 71   # 门槛：MIN_HISTORY+1，find_start 的硬下界

# ---------------- 换手 ----------------
TURN_WINDOW = 20      # 换手率统计窗口（交易日）
TURN_MIN_COVER = 15   # 窗口内至少覆盖这么多天，否则视为数据不足
TURN_PCT = 0.0        # 换手默认只排序标注、不剔除；设 0.4 = 硬过滤保留池内最低 40%
TURN_MIN_N = 5        # 名单少于该数 → 自动放宽到 60%
TURN_MAX_N = 30       # 名单多于该数 → 自动收紧到 20%

# ---------------- 业绩转变标注（只标注，不裁决） ----------------
TURN_GM_REQUIRE_YOY = True   # ① 同比护栏：最新单季毛利率须高于上年同季
NEW_STOCK_YEARS = 3.0        # ⑤ 次新 = 上市不足 3 年（用 stock.list_date 判）

# ---------------- 输出 ----------------
# 2026-11-15 由 "stock_kline_screen" 改为 "kline-screen"，与另外两个工具
# （quality-screen / cycle-elastic）统一用连字符：
#   output/quality-screen/  output/cycle-elastic/  output/kline-screen/
# 这纯粹是目录命名一致性，不涉及产物内容。改的时候同步了 compare.py、
# README.md 和 tests/test_pipeline.py 里断言这个路径的地方（共 6 处）。
OUT_DIR_NAME = "kline-screen"

CSV_HEADER = ["代码", "名称", "一级行业", "启动日", "距启动交易日",
              "启动日量能倍数", "启动日涨幅%", "距MA20%", "距MA60%",
              "毛利率改善",
              "20日换手%", "池内换手分位", "分位星级",
              "最新收盘价", "总市值（亿）"]
"""15 列。相对原版（16 列）删了「二级行业」：共享库的 `stock.industry` 只有上游
一级口径，没有申万二级。想恢复这列得先给 hub 加二级行业来源，不能拿一级凑数。"""

RESULT_VERSION = "v2.0"
