#!/usr/bin/env python3
"""A4.2a 量化影响测量：FP32 与 INT8 的**排序一致率**（不依赖检索库，样本更大）。

为什么单独做这一项：sanity set 只有 12 条、RAG 用例以词法为主，两者都难以充分
暴露量化差异。这里用「同义改写 + 不相关」构造一个规模的文档/查询矩阵，直接问：

    INT8 排出来的 Top-1 / Top-3 与 FP32 是否一致？cosine 掉了多少？

§12 明确：量化不要求逐元素数值一致，看 **retrieval behaviour**。所以主指标是
排序一致率，而不是向量距离。

用法：<venv>/python.exe tools/embedding_eval/agreement.py
"""
from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import OnnxBertEmbedder          # noqa: E402

EVAL_DIR = REPO / "runtime" / "models" / "_eval"

#: 文档池：现有 RAG 语料的真实段落 + sanity 文档（都是产品形态的中文文本）
DOCS = [
    ("cn_rag_long", "父块用于回答，子块用于召回；检索命中子块后回溯到父块作为上下文。"),
    ("cn_chunker_short", "分块时保留语义边界，避免把一句话从中间切开；重叠区域用来兜住跨界语义。"),
    ("index_fallback", "索引重建不会改动笔记原文，只会重建派生索引；重建期间检索仍然可用。"),
    ("multi_section_manual", "上下文的注入预算一旦封顶，命中再多块也只取前若干段，避免小模型被长文淹没。"),
    ("postgres_note", "PostgreSQL 的并发控制在多版本快照上实现，读不阻塞写。"),
    ("postgrey_note", "灰度发布期间新旧版本并存，需要注意接口兼容与数据双写。"),
    ("typhoon_weather", "台风路径预报存在不确定性，官方会多次修正登陆点预测。"),
    ("typhoon_fighter", "战斗机在强对流天气下的起飞窗口受到严格限制。"),
    ("person_zhang", "张工负责数据平台，主要处理离线任务与调度。"),
    ("person_alan", "Alan 负责前端体验，关注首屏加载与交互流畅度。"),
    ("product_krevix", "Krevix 净水器的滤芯寿命约 6 个月，更换后需要复位计时器。"),
    ("product_krevix_clone", "Krevix 净水器仿制品的滤芯接口不兼容，强行安装会漏水。"),
    ("numbers_report", "今年第三季度营收 1234567.89 元，同比增长 12.5%。"),
    ("en_vector_index", "An approximate nearest neighbour index trades recall for latency."),
    ("en_short_note", "Short notes are easier to review later."),
    ("person_zhang_kpi", "张工的季度目标是把任务失败率降到千分之一以下。"),
    ("booking_flow", "下单后十五分钟内未支付，订单会被自动取消并释放库存。"),
    ("refund_policy", "退款在收货后七天内可申请，超过期限只能走人工审核。"),
    ("meeting_minutes", "会议决定把数据迁移延期两周，风险是老客户端未升级。"),
    ("ops_switch", "主库不可写时先判断磁盘是否写满，再执行主从切换。"),
    ("storage_design", "文件指纹用修改时间与大小组成，精度不足时退化为内容哈希。"),
    ("product_manual", "睡眠模式下噪音约 22 分贝，指示灯熄灭，适合卧室使用。"),
    ("paper_attention", "位置编码让自注意力获得序列顺序信息，原始论文用正弦余弦构造。"),
    ("docker_note", "镜像分层复用了只读层，容器启动只增加可写层。"),
    ("k8s_note", "滚动更新会逐步替换 Pod，就绪探针未通过时不会接入流量。"),
    ("cache_note", "缓存失效策略决定命中率，也决定后端在峰值时的压力。"),
    ("api_versioning", "接口版本放在路径里比放在请求头更容易排查问题。"),
    ("log_retention", "日志保留周期与磁盘容量直接相关，长周期需要滚动归档。"),
    ("backup_rule", "备份要能恢复才算备份，未演练过的备份等于没有备份。"),
    ("test_strategy", "回归用例的价值在于稳定拦住退化，而不是覆盖所有分支。"),
]

#: 查询池：同义表达 / 简称 / 自然问句 / 英文，覆盖产品真实提问形态
QUERIES = [
    ("过滤网多久换一次", "product_krevix"),
    ("净水器滤芯寿命", "product_krevix"),
    ("晚上吵不吵", "product_manual"),
    ("睡觉时有没有噪音", "product_manual"),
    ("两个文件只差一个字能发现吗", "storage_design"),
    ("文件指纹怎么算", "storage_design"),
    ("这周性能优化了什么", "meeting_minutes"),
    ("每秒请求数提高了多少", "meeting_minutes"),
    ("哪些目标没按时完成", "meeting_minutes"),
    ("老客户端不升级有什么麻烦", "meeting_minutes"),
    ("硬盘满了写不进去", "ops_switch"),
    ("数据库写不了怎么办", "ops_switch"),
    ("数据文件坏了先做什么", "ops_switch"),
    ("不联网也能用的软件", "en_vector_index"),
    ("资料放在自己电脑上的代价", "en_vector_index"),
    ("订单没支付会怎样", "booking_flow"),
    ("超过七天还能退吗", "refund_policy"),
    ("任务失败率降到多少", "person_zhang_kpi"),
    ("谁负责数据平台", "person_zhang"),
    ("谁管前端体验", "person_alan"),
    ("索引重建会不会改原文", "index_fallback"),
    ("注入预算为什么要封顶", "multi_section_manual"),
    ("子块和父块分别干什么", "cn_rag_long"),
    ("分块为什么要有重叠", "cn_chunker_short"),
    ("镜像怎么复用层", "docker_note"),
    ("滚动更新什么时候接流量", "k8s_note"),
    ("缓存命中率低会怎样", "cache_note"),
    ("接口版本放哪里", "api_versioning"),
    ("日志保留多久合适", "log_retention"),
    ("备份怎么才算有效", "backup_rule"),
    ("回归用例的价值", "test_strategy"),
    ("台风登陆点准不准", "typhoon_weather"),
    ("战机强对流天气能起飞吗", "typhoon_fighter"),
    ("营收增长多少", "numbers_report"),
    ("位置编码干什么用", "paper_attention"),
    ("并发控制怎么实现", "postgres_note"),
    ("灰度期间要注意什么", "postgrey_note"),
    ("仿冒品滤芯能装吗", "product_krevix_clone"),
    ("approximate nearest neighbour tradeoff", "en_vector_index"),
    ("short notes review", "en_short_note"),
]


def _cos(a, b) -> float:
    import math
    d = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return d / (na * nb or 1.0)


def _load(cid: str) -> OnnxBertEmbedder:
    inputs = json.loads((EVAL_DIR / "selection_inputs.json").read_text(encoding="utf-8"))
    cand = next(c for c in inputs["candidates"] if c["id"] == cid)
    emb = OnnxBertEmbedder(REPO / cand["local_path"],
                           EVAL_DIR / "tokenizers" / cand["repo"].replace("/", "__"),
                           model_name=cid)
    emb._lazy()
    return emb


def _ranking(emb, docs, queries) -> list[list[int]]:
    dv = emb.embed([d[1] for d in docs])
    qv = emb.embed([q[0] for q in queries])
    out = []
    for v in qv:
        sims = sorted(range(len(dv)), key=lambda i: -_cos(v, dv[i]))
        out.append(sims)
    return out


def main() -> int:
    print(f"文档池 {len(DOCS)} 篇 · 查询池 {len(QUERIES)} 条 · "
          f"对比对 {len(DOCS)*len(QUERIES)} 组\n")

    rows = {}
    for cid in ("xenova-fp32", "xenova-int8", "qdrant-fp32-opt"):
        emb = _load(cid)
        rank = _ranking(emb, DOCS, QUERIES)
        # 自评准确率：Top1 是否命中标注文档
        hit1 = sum(1 for (q, exp), r in zip(QUERIES, rank)
                   if DOCS[r[0]][0] == exp)
        rows[cid] = {"rank": rank, "top1_acc": hit1 / len(QUERIES)}
        print(f"[{cid:18}] Top1 命中标注 = {hit1}/{len(QUERIES)} "
              f"({hit1/len(QUERIES)*100:.1f}%)")

    base = rows["xenova-fp32"]["rank"]
    print("\n与 FP32（xenova-fp32）的排序一致率：")
    for cid in ("xenova-int8", "qdrant-fp32-opt"):
        r = rows[cid]["rank"]
        # Top1 一致率／Top3 集合一致率（顺序无关，衡量「同样的候选是否被选中」）
        same1 = sum(1 for a, b in zip(base, r) if a[0] == b[0])
        same3 = sum(1 for a, b in zip(base, r) if set(a[:3]) == set(b[:3]))
        # 全集上的平均 Kendall 距离近似：相邻两两顺序一致比例
        agree_pairs = 0
        total_pairs = 0
        for a, b in zip(base, r):
            pos_a = {doc: i for i, doc in enumerate(a)}
            pos_b = {doc: i for i, doc in enumerate(b)}
            for x, y in combinations(pos_a, 2):
                total_pairs += 1
                if (pos_a[x] < pos_a[y]) == (pos_b[x] < pos_b[y]):
                    agree_pairs += 1
        print(f"  {cid:18} Top1 {same1}/{len(QUERIES)} ({same1/len(QUERIES)*100:.1f}%)   "
              f"Top3 集合一致 {same3}/{len(QUERIES)} ({same3/len(QUERIES)*100:.1f}%)   "
              f"成对顺序一致 {agree_pairs/total_pairs*100:.2f}%")

    # 每个候选的失败样例（对报告有用）
    for cid in rows:
        miss = [(q, exp, DOCS[rows[cid]["rank"][i][0]][0])
                for i, (q, exp) in enumerate(QUERIES)
                if DOCS[rows[cid]["rank"][i][0]][0] != exp]
        if miss:
            print(f"\n[{cid}] Top1 未命中标注（{len(miss)}）：")
            for q, exp, got in miss[:8]:
                print(f"    {q:34} 期望 {exp:22} 实为 {got}")

    out = EVAL_DIR / "agreement.json"
    out.write_text(json.dumps(
        {cid: {"top1_acc": rows[cid]["top1_acc"],
               "top1": [DOCS[i[0]][0] for i in rows[cid]["rank"]]}
         for cid in rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
