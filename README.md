# Pantryfifo · 冰箱临期先吃

分批入库 → 暂存确认落层 → FEFO 扣减 → 过期下架。

入库先落暂存区(`POST /api/lots` → `GET /api/staging`),确认落层
(`POST /api/staging/{id}/confirm`)才写入 lots 上架;过期判定全系统只有
一条规则(`expiry < today`),顶条、过期下架、按临期消费三处资格一致。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

0-1：`shopping_list` / `recipe_suggest`;已落地：`temp_zone`。
