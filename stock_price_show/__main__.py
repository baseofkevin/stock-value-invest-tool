"""`python3 -m stock_price_show` 的入口。

单独一个文件是为了让"怎么跑"和"跑什么"分开：
`python3 -m stock_price_show.cli` 也能跑，但入口只该有一个。
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
