#!/usr/bin/env python

from setuptools import setup, find_packages


setup(name='actionban',
      version='0.1.0',
      description='A action based banning system',
      author='Isaac Cook',
      author_email='isaac@simpload.com',
      url='http://www.python.org/sigs/distutils-sig/',
      packages=find_packages(),
      entry_points={
          'console_scripts': [
              'actionban = actionban.main:main'
          ]
      }
      )
