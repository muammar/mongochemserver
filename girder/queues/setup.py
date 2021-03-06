from setuptools import setup, find_packages

setup(
    name='openchemistry-queues',
    version='0.1.0',
    description='Queue system for taskflows',
    packages=find_packages(),
    install_requires=[
      'girder>=3.0.0a5',
      # Add these in when they have been modified for use with girder 3.x
      'cumulus-plugin',
      'cumulus-taskflow'
    ],
    entry_points={
      'girder.plugin': [
          'queues = queues:QueuePlugin'
      ]
    }
)
