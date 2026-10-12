from setuptools import setup

setup(name='paperbroker',
      version='0.1.3',
      description='PaperBroker',
      author='Philip ODonnell',
      author_email='philip@postral.com',
      url='https://github.com/philipodonnell/paperbroker',
      packages=['paperbroker',
               'paperbroker.adapters',
               'paperbroker.adapters.quotes',
               'paperbroker.logic',
               ],
        install_requires=[
            'arrow',
            'requests',
            # B-13: the server imports flask; requirements.txt already had it
            'flask>=3.1'
        ],
        # B-13: templates/ holds screen.html but is not a python package
        # (no __init__.py), so it needs an explicit data declaration
        package_data={
            'paperbroker': ['templates/*.html']
        }
     )
