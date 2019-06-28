from __future__ import absolute_import

import textwrap

from tests.lib import _create_main_file, _git_commit


def _create_test_package_submodule(env):
    env.scratch_path.join("version_pkg_submodule").mkdir()
    submodule_path = env.scratch_path / 'version_pkg_submodule'
    env.run('touch', 'testfile', cwd=submodule_path)
    env.run('git', 'init', cwd=submodule_path)
    env.run('git', 'add', '.', cwd=submodule_path)
    _git_commit(env, submodule_path, message='initial version / submodule')

    return submodule_path


def _change_test_package_submodule(env, submodule_path):
    submodule_path.join("testfile").write("this is a changed file")
    submodule_path.join("testfile2").write("this is an added file")
    env.run('git', 'add', '.', cwd=submodule_path)
    _git_commit(env, submodule_path, message='submodule change')


def _pull_in_submodule_changes_to_module(env, module_path, rel_path):
    """
    Args:
      rel_path: the location of the submodule relative to the superproject.
    """
    submodule_path = module_path / rel_path
    env.run('git', 'pull', '-q', 'origin', 'master', cwd=submodule_path)
    # Pass -a to stage the submodule changes that were just pulled in.
    _git_commit(env, module_path, message='submodule change', args=['-a'])


def _create_test_package_with_submodule(env, rel_path):
    """
    Args:
      rel_path: the location of the submodule relative to the superproject.
    """
    env.scratch_path.join("version_pkg").mkdir()
    version_pkg_path = env.scratch_path / 'version_pkg'
    version_pkg_path.join("testpkg").mkdir()
    pkg_path = version_pkg_path / 'testpkg'

    pkg_path.join("__init__.py").write("# hello there")
    _create_main_file(pkg_path, name="version_pkg", output="0.1")
    version_pkg_path.join("setup.py").write(textwrap.dedent('''\
                        from setuptools import setup, find_packages
                        setup(name='version_pkg',
                              version='0.1',
                              packages=find_packages(),
                             )
                        '''))
    env.run('git', 'init', cwd=version_pkg_path, expect_error=True)
    env.run('git', 'add', '.', cwd=version_pkg_path, expect_error=True)
    _git_commit(env, version_pkg_path, message='initial version')

    submodule_path = _create_test_package_submodule(env)

    env.run(
        'git',
        'submodule',
        'add',
        submodule_path,
        rel_path,
        cwd=version_pkg_path,
        expect_error=True,
    )
    _git_commit(env, version_pkg_path, message='initial version w submodule')

    return version_pkg_path, submodule_path
