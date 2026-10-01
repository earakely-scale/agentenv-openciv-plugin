from agentenv_openciv3.recording import spread


def test_spread_leaves_distant_labels_alone():
    assert spread([10, 50, 90], 12, 0, 100) == [10, 50, 90]


def test_spread_separates_close_labels_in_order():
    ys = spread([40, 45, 41], 12, 0, 100)
    assert ys[0] == 40 and ys[2] == 52 and ys[1] == 64


def test_spread_keeps_labels_above_the_bottom():
    assert spread([98, 99], 12, 0, 100) == [88, 100]
