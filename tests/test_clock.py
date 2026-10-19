"""时钟同步: NTP 四时间戳公式与 offset 符号约定。

offset 的符号最容易搞反, 一旦反了音频会整体偏移两倍网络延迟, 且听感上
只是"有点滞后", 很难定位。这里用固定向量把语义钉死。
"""

from __future__ import annotations

from com_bridge.clock import ClockSync, JitterBufferController, now_us


class TestClockSyncFormula:
    """offset = remote_clock - local_clock(对端减本地)。

    下面每个向量都同时写清"物理场景"和"手算过程", 因为符号约定写反时
    数值看起来同样合理, 只有对着物理场景才能判对错。
    """

    def test_symmetric_delay_gives_zero_offset(self):
        # 两机时钟一致, 单程延迟 500us
        # t1=1000 本地发, t2=1500 对端收, t3=1600 对端发, t4=2100 本地收
        # rtt    = (2100-1000) - (1600-1500) = 1000
        # offset = ((1500-1000) + (1600-2100)) / 2 = (500-500)/2 = 0
        cs = ClockSync()
        cs.update(1000, 1500, 1600, 2100)
        assert cs.offset_us == 0.0
        assert cs.rtt_us == 1000.0
        assert cs.local_to_remote(2000) == 2000
        assert cs.remote_to_local(2000) == 2000

    def test_remote_clock_ahead_by_100(self):
        """对端时钟比本地快 100us, 单程延迟 500us, 对端处理 100us。

        t1 = 1000                本地发(本地钟)
        t2 = 1000+500+100 = 1600 对端收(对端钟: 单程 500 + 超前 100)
        t3 = 1700                对端回(对端侧又过了 100)
        t4 = 1700-100+500 = 2100 本地收(对端发回后过 500us 单程;
                                 换算回本地钟 = 2200-100 = 2100)

        rtt    = (2100-1000) - (1700-1600) = 1000 = 2×单程 ✓
        offset = ((1600-1000) + (1700-2100)) / 2 = (600-400)/2 = 100 = C ✓

        注意 t4 必须把对端的处理时间也算进去, 否则向量自身矛盾
        (曾写成 t4=2000, 于是 rtt 算出 900 而不是 1000)。
        """
        cs = ClockSync()
        cs.update(1000, 1600, 1700, 2100)
        assert cs.offset_us == 100.0
        assert cs.rtt_us == 1000.0
        # offset = remote - local > 0: 同一时刻对端读数更大
        assert cs.local_to_remote(2000) == 2100
        assert cs.remote_to_local(2000) == 1900

    def test_remote_clock_behind_by_200(self):
        """对端时钟比本地慢 200us, 单程延迟 500us。

        t1 = 1000               本地发
        t2 = 1000+500-200=1300  对端收
        t3 = 1300               对端立即回
        t4 = 2000               本地收

        rtt    = (2000-1000) - 0 = 1000
        offset = ((1300-1000) + (1300-2000)) / 2 = (300-700)/2 = -200
        """
        cs = ClockSync()
        cs.update(1000, 1300, 1300, 2000)
        assert cs.offset_us == -200.0
        assert cs.rtt_us == 1000.0
        assert cs.local_to_remote(2000) == 1800
        assert cs.remote_to_local(2000) == 2200

    def test_documented_derivation_vector(self):
        """模块 docstring 里那组推导: 对端快 C=1000, 单程 d=10。

        t1=5000, t2=6010, t3=6010, t4=5020 -> offset=1000, 换算回对端时间
        应得 5000+1000=6000。
        """
        cs = ClockSync()
        cs.update(5000, 6010, 6010, 5020)
        assert cs.offset_us == 1000.0
        assert cs.local_to_remote(5000) == 6000

    def test_median_filter_rejects_outlier(self):
        cs = ClockSync()
        for _ in range(7):
            cs.update(1000, 1600, 1700, 2000)      # offset = 150
        cs.update(1000, 999999, 999999, 2000)      # 明显异常的样本
        assert cs.offset_us == 150.0, "中位数滤波应把离群样本排掉"

    def test_synced_requires_enough_samples(self):
        cs = ClockSync()
        assert not cs.synced
        for _ in range(4):
            cs.update(0, 0, 0, 0)
        assert cs.synced

    def test_history_is_bounded(self):
        cs = ClockSync()
        for i in range(100):
            cs.update(i, i, i, i)
        assert len(cs.history) <= 16
        assert cs.samples == 100


class TestJitterBufferController:
    def test_target_stays_within_bounds(self):
        jb = JitterBufferController(min_ms=20.0, max_ms=200.0)
        for level, jitter in ((0.0, 0.0), (0.0, 500.0), (10000.0, 0.0),
                              (60.0, 5.0)):
            t = jb.update(level, jitter)
            assert 20.0 <= t <= 200.0

    def test_high_jitter_raises_target(self):
        jb = JitterBufferController(min_ms=20.0, max_ms=200.0)
        start = jb.target_ms
        for _ in range(200):
            jb.update(60.0, 200.0)
        assert jb.target_ms > start


class TestNowUs:
    def test_monotonic_and_microsecond_scale(self):
        a = now_us()
        b = now_us()
        assert b >= a
        # 微秒量级: 1e12 以上意味着误用了纳秒或秒
        assert a > 10**9
