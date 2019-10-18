import pytest
import src.ComplexNums as ComplexNums
#from myproj import ComplexNums

class TestComplex():

    def test_cc(self):
        z = ComplexNums.Complex(3.0, 4.0)
        zcc = z.cc()
        assert zcc.imag == -z.imag
        assert zcc.real == z.real

    def test_mag(self):
        z = ComplexNums.Complex(3.0, 4.0)
        assert z.magnitude() == 5.0
        assert z.r == z.magnitude()

    def test_angle(self):
        z = ComplexNums.Complex(3.0, 4.0)
        assert z.angle() == 0.9272952180016122
        assert z.theta == z.angle()
