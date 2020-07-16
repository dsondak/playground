import numpy as np

class Complex:

    def __init__(self, a, b):
        self.real = a
        self.imag = b

    def __repr__(self):
        raise NotImplementedError

    def cc(self):
        return Complex(self.real, -self.imag)

    def magnitude(self):
        self.r = np.sqrt(self.real * self.real + self.imag * self.imag)
        return self.r

    def angle(self):
        self.theta = np.arctan(self.imag / self.real)
        return self.theta

    def __add__(self, other):
        try:
           return Complex(self.real + other.real, self.imag + other.imag)
        except AttributeError:
           return Complex(self.real + other, self.imag)

    def __radd__(self, other):
        return Complex.__add__(self, other)
