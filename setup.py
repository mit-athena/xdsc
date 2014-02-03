#!/usr/bin/python

from distutils.core import setup

setup(name='xdsc',
      version='10.1',
      description='Gtk 3 graphical client for Athena discuss',
      author='Jonathan Reed',
      maintainer='Debathena Project',
      maintainer_email='debathena@mit.edu',
      scripts=['xdsc'],
      data_files=[('/usr/share/xdsc', ['xdsc.ui', 'xdsc_icon.gif'])],
    )
