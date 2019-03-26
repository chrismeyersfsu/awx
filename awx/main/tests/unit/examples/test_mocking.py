
from unittest import mock
import pytest

from django.conf import settings

from modules.earth import Earth
from modules.dog import Dog
from modules.cat import Cat

'''
This is better than trying to use the @mock.patch decorator atop test_ functions.
'''
@pytest.fixture
def patch_Dog():
    with mock.patch('modules.earth.Dog'):
        yield


'''
Mock module usage of another module
'''
def test_mock_module_usage(patch_Dog):
    earth = Earth()
    dog = earth.generate_dog()

    assert isinstance(dog, mock.MagicMock)


'''
Context manage is not prefered because the nested can get ugly
'''
def test_mock_module_not_prefered():
    earth = Earth()
    dog = earth.generate_dog()
    cat = earth.generate_cat()
    assert isinstance(dog, Dog)
    assert isinstance(cat, Cat)

    with mock.patch('modules.earth.Dog'):
        with mock.patch('modules.earth.Cat'):
            dog = earth.generate_dog()
            cat = earth.generate_cat()

            assert isinstance(dog, mock.MagicMock)
            assert isinstance(cat, mock.MagicMock)


'''
Get a handle on a constructor Mock
'''
@pytest.fixture
def patch_Dog():
    with mock.patch('modules.earth.Dog') as d:
        d.return_value = d
        yield d


def test_mock_obj_returned_by_constructor(patch_Dog):
    earth = Earth()
    assert 0 == patch_Dog.call_count

    dog = earth.generate_dog()

    # Constructor
    assert isinstance(dog, mock.MagicMock)
    assert isinstance(patch_Dog, mock.MagicMock)
    assert 1 == patch_Dog.call_count
    assert dog == patch_Dog

    # Obj returned by constructor
    assert 0 == patch_Dog.speak.call_count
    dog.speak()
    assert 1 == patch_Dog.speak.call_count


'''
Mock Django settings. If you just change settings.<x> it will remain changed across
tests.
'''
def test_settings_incorrect_1():
    assert hasattr(settings, 'FOO') is False
    settings.FOO = 'bar'
    assert hasattr(settings, 'FOO') is True


def test_settings_incorrect2():
    assert hasattr(settings, 'FOO') is True
    assert 'bar' == settings.FOO


def test_settings_correct1(settings):
    assert hasattr(settings, 'FOOBAR') is False
    settings.FOOBAR = 'foobar'
    assert hasattr(settings, 'FOOBAR') is True


def test_settings_correct2(settings):
    assert hasattr(settings, 'FOOBAR') is False



