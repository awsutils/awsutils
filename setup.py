from setuptools import find_packages, setup


setup(
    name="awsutils",
    version="0.1.0",
    description="Small AWS CLI utility commands.",
    packages=find_packages(),
    entry_points={
        "console_scripts": ["awsutils=awsutils.cli:main"],
    },
)
