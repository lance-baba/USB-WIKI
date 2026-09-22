# USB-WIKI · Attribution Scope Router Spike V1

- 生成：2026-09-23 07:28:42
- Router DEV：**72 题**（GUARD 36 / PASS_THROUGH 36，边界负例 50%） 领域 {'工程': 20, '天气': 16, '设备': 21, '制度': 15}
- 纯确定性正则，**无 LLM / embedding / reranker / ONNX / 网络**；未改 app/。

## Router 指标

| 指标 | 值 | 门槛 |
| --- | --- | --- |
| Router Precision | **100.0%** | ≥98% ✅ |
| Router Recall（高三 risk） | **100.0%** | ≥95% ✅ |
| Router F1 | **1.000** | — |
| **False Route Rate**（普通题误进 Guard） | **0.0%** | ≤2% ✅ |
| DEV 72 unnecessary guard rate | **1.4%** | ≤2% ✅ |
| Attribution DEV 高三 risk 覆盖率 | **100.0%** | ≥95% ✅ |
| Router latency | **0.0042 ms/query** | <1ms ✅ |

## 组合（Router + Guard）—— 最终产品语义

| 指标 | 值 |
| --- | --- |
| Router+Guard False Association | **0.0%** |
| Positive Relation Accuracy | **100.0%** |
| Negative Relation Accuracy | 100.0% |
| Abstention Accuracy | 100.0% |
| **Unnecessary Block Rate（有 router）** | **0.0%** |
| Unnecessary Block Rate（**若无 router**） | **91.7%** |

被 router 正确拦下、否则会被 guard 误杀的正常题：

- `rt_p01` 大坝的设计库容是多少？ → guard 本会判 `INSUFFICIENT_RELATION`
- `rt_p02` 坝顶高程是多少？ → guard 本会判 `INSUFFICIENT_RELATION`
- `rt_p06` 项目负责人的职责是什么？ → guard 本会判 `INSUFFICIENT_RELATION`
- `rt_p07` 监测组长的职责是什么？ → guard 本会判 `INSUFFICIENT_RELATION`
- `rt_p08` 数据处理员负责什么工作？ → guard 本会判 `INSUFFICIENT_RELATION`
- `rt_p09` RT 系列采集仪是什么型号？ → guard 本会判 `INSUFFICIENT_RELATION`
- `rt_p10` 采集仪支持哪些功能？ → guard 本会判 `INSUFFICIENT_RELATION`
- `rt_p11` RT 采集仪用于什么？ → guard 本会判 `INSUFFICIENT_RELATION`
- `rt_p12` 采集仪有哪些型号？ → guard 本会判 `INSUFFICIENT_RELATION`
- `rt_p13` 本轮共接报多少起灾情？ → guard 本会判 `INSUFFICIENT_RELATION`
- `rt_p14` 8 月共发布多少次预警？ → guard 本会判 `INSUFFICIENT_RELATION`
- `rt_p15` 停电影响了多少个村？ → guard 本会判 `INSUFFICIENT_RELATION`

## DEV 72 路由情况

- 路由进 Guard：3 道 ['jkj_df_data_person', 'wea_at_temp_drop', 'wea_at_typhoon_frost']
- **多余进入 Guard：1 道（1.4%）**
  - `jkj_df_data_person` [direct_fact] 谁负责数据处理？

## Attribution DEV 覆盖情况

- 高三 risk（causal/event/responsibility）：24 题，路由进 Guard **24** 题（100.0%）
- 漏掉：无
- 非高三 risk 却被路由进 Guard：无

---

> Router 只决定「要不要交给 Guard」；Guard 只做拦截与标注，不回答。
> 本轮未修改 app/，未调用任何模型。
