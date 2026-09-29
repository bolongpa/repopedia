import os
from pkg.util import helper


class Base:
    def name(self):
        return "base"


class Engine(Base):
    def run(self, task):
        cleaned = helper(task)
        self.log(cleaned)
        return cleaned

    def log(self, msg):
        print(msg)


def launch():
    engine = Engine()
    engine.run("hello")
