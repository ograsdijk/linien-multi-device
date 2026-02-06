from app.serializers import to_jsonable, UNSERIALIZABLE
import numpy as np


def test_to_jsonable_basic():
    assert to_jsonable(1) == 1
    assert to_jsonable(1.5) == 1.5
    assert to_jsonable(True) is True
    assert to_jsonable("x") == "x"


def test_to_jsonable_numpy():
    arr = np.array([1, 2, 3])
    assert to_jsonable(arr) == [1, 2, 3]
    assert to_jsonable(np.int64(5)) == 5


def test_to_jsonable_bytes():
    assert to_jsonable(b"abc") is UNSERIALIZABLE


