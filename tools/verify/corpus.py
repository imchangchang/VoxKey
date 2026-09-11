"""S1 测试语料定义：三组（中文 / 英文 / 中英混读）。这是语料的唯一来源——
`gen_corpus.sh` 读这份清单生成 wav + transcripts.tsv，run_verify.py 只认生成出来的 tsv。

每条 = (文件名, 文本, 组, TTS 语音)。
语音用 macOS say 生成 TTS 基线，只用于先把流水线跑通；
正式验收（issue #1 的三组达标线）必须用真人录音重测，TTS 结果偏乐观。
"""

CORPUS = [
    # ---- 纯中文（Tingting 朗读）----
    ("zh_01", "今天的会议改到下午三点钟", "zh", "Tingting"),
    ("zh_02", "帮我把这个文件发给项目经理", "zh", "Tingting"),
    ("zh_03", "明天早上我要先去一趟银行", "zh", "Tingting"),
    ("zh_04", "这个功能什么时候可以上线", "zh", "Tingting"),
    ("zh_05", "楼下新开的咖啡店味道还不错", "zh", "Tingting"),
    ("zh_06", "记得提醒我周五之前交周报", "zh", "Tingting"),
    ("zh_07", "这个问题我回头再仔细看看", "zh", "Tingting"),
    ("zh_08", "快递到了的话帮我签收一下", "zh", "Tingting"),
    ("zh_09", "最近天气变化大注意别感冒", "zh", "Tingting"),
    ("zh_10", "我们下周讨论一下新版本的需求", "zh", "Tingting"),
    # ---- 纯英文（Samantha 朗读）----
    ("en_01", "the meeting has been moved to three pm", "en", "Samantha"),
    ("en_02", "please send this file to the project manager", "en", "Samantha"),
    ("en_03", "I will review the pull request this afternoon", "en", "Samantha"),
    ("en_04", "can you hear me clearly now", "en", "Samantha"),
    ("en_05", "let's schedule a call for tomorrow morning", "en", "Samantha"),
    ("en_06", "the new feature will be released next week", "en", "Samantha"),
    ("en_07", "I need to finish this report by Friday", "en", "Samantha"),
    ("en_08", "what time does the flight arrive", "en", "Samantha"),
    # ---- 中英混读（Tingting 朗读，内嵌英文）----
    ("mix_01", "这个 bug 我明天 fix 一下", "mix", "Tingting"),
    ("mix_02", "先把代码 push 到 main 分支", "mix", "Tingting"),
    ("mix_03", "今天的 standup 改成下午两点", "mix", "Tingting"),
    ("mix_04", "帮我 check 一下这个 PR 有没有冲突", "mix", "Tingting"),
    ("mix_05", "这个 API 的文档还没写完", "mix", "Tingting"),
    ("mix_06", "release 之前记得跑一遍 test", "mix", "Tingting"),
    ("mix_07", "我用 Python 写了个脚本处理数据", "mix", "Tingting"),
    ("mix_08", "deadline 是下周五大家抓紧", "mix", "Tingting"),
]
