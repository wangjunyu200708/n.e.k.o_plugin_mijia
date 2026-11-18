"""多指令拆分（nlp/splitter.py）的纯函数测试。

拆分的判定标准只有一条：**切完能不能被现有路由接住**。所以这里测的是
切分结果本身（段数、内容、顺序），接不住的情况由 test_multi_command.py
的预检用例兜——那里会验证"切坏了就整句回退"。
"""

from nlp.splitter import LEAD_VERB_RE, inherit_verb, split_commands


class TestSingleCommandUntouched:
    """单指令必须原样返回，任何清理都不能改变它。"""

    def test_plain_command_returned_verbatim(self):
        assert split_commands("打开卧室灯") == ["打开卧室灯"]

    def test_command_with_query_suffix_not_split(self):
        assert split_commands("关闭卧室灯怎么样") == ["关闭卧室灯怎么样"]

    def test_scene_command_not_split(self):
        assert split_commands("执行回家场景") == ["执行回家场景"]

    def test_degree_adverb_not_split(self):
        """单字"再"不作分隔符：'灯亮度再高一点' 是一句话，不是两句。"""
        assert split_commands("灯亮度再高一点") == ["灯亮度再高一点"]
        assert split_commands("温度再调高点") == ["温度再调高点"]

    def test_empty_input(self):
        assert split_commands("") == [""]
        assert split_commands(None) == [""]


class TestPunctuationSplit:
    def test_comma(self):
        assert split_commands("打开客厅灯，关掉插座") == ["打开客厅灯", "关掉插座"]

    def test_ascii_comma(self):
        assert split_commands("打开客厅灯,关掉插座") == ["打开客厅灯", "关掉插座"]

    def test_enumeration_comma(self):
        """顿号最常见于列举设备：'打开客厅灯、卧室灯'。"""
        assert split_commands("打开客厅灯、卧室灯") == ["打开客厅灯", "卧室灯"]

    def test_semicolon(self):
        assert split_commands("打开客厅灯；关掉插座") == ["打开客厅灯", "关掉插座"]


class TestConnectorSplit:
    def test_then(self):
        assert split_commands("开灯然后关窗帘") == ["开灯", "关窗帘"]

    def test_after_that(self):
        assert split_commands("打开客厅灯接着关掉插座") == ["打开客厅灯", "关掉插座"]

    def test_by_the_way(self):
        assert split_commands("帮我打开客厅灯，顺便关掉插座") == ["打开客厅灯", "关掉插座"]

    def test_conjunction(self):
        assert split_commands("打开客厅灯和卧室灯") == ["打开客厅灯", "卧室灯"]


class TestCleanup:
    def test_ba_prefix_in_second_segment(self):
        """整句开头的"把"已剥，段内的"把"也要剥，否则设备名变成"把客厅灯"。"""
        assert split_commands("关掉插座，把客厅灯调到50%") == ["关掉插座", "客厅灯调到50%"]

    def test_ye_before_verb(self):
        """'卧室灯也关掉' → '卧室灯关掉'，否则"也"被算进设备名。"""
        assert split_commands("把客厅灯打开，卧室灯也关掉") == ["客厅灯打开", "卧室灯关掉"]

    def test_trailing_particle(self):
        assert split_commands("打开客厅灯吧，关掉插座吧") == ["打开客厅灯", "关掉插座"]

    def test_verb_tail_kept(self):
        """结尾的"了"属于动词，不能当语气词剥掉。"""
        assert split_commands("卧室电视关了") == ["卧室电视关了"]


class TestDegenerateSplitFallsBack:
    """切出来的段数不足 2 时，必须返回原始文本而不是切碎的半句。"""

    def test_device_name_containing_conjunction(self):
        """'和风空调' 被切成 ['打开', '风空调']，但只有两段非空…

        这里只断言行为稳定：切了。能不能用由调用方预检决定——预检会发现
        '打开' 路由不了，整句回退。此处锁住切分结果，防止后续改正则时
        悄悄换行为而没人发现。
        """
        assert split_commands("打开和风空调") == ["打开", "风空调"]

    def test_trailing_separator_does_not_create_extra_segment(self):
        """只有一段有效内容时返回原文——调用方只在段数 >1 时才用返回值。"""
        assert len(split_commands("打开客厅灯，")) == 1


class TestLeadVerb:
    def test_matches_switch_verbs(self):
        for verb in ("打开", "关闭", "关掉", "开", "关"):
            assert LEAD_VERB_RE.match(f"{verb}客厅灯"), verb

    def test_matches_action_verbs(self):
        assert LEAD_VERB_RE.match("开始扫地")
        assert LEAD_VERB_RE.match("回充")

    def test_does_not_match_bare_device(self):
        assert LEAD_VERB_RE.match("卧室灯") is None


class TestInheritVerb:
    def test_prepends_shared_verb(self):
        assert inherit_verb("卧室灯", "打开") == "打开卧室灯"

    def test_keeps_segment_that_has_verb(self):
        assert inherit_verb("关掉插座", "打开") == "关掉插座"

    def test_no_verb_available(self):
        assert inherit_verb("卧室灯", "") == "卧室灯"

    def test_empty_segment(self):
        assert inherit_verb("", "打开") == ""
