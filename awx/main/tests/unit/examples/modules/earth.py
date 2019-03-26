
from .dog import Dog
from .cat import Cat


class Earth():
    def __init__(self):
        self.dogs = []
        self.cats = []

    def generate_dog(self):
        dog = Dog()
        self.dogs.append(dog)
        return dog

    def generate_cat(self):
        cat = Cat()
        self.cats.append(cat)
        return cat

