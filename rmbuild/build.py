
import datetime
import shlex
import functools

from .compat import *

from . import util
from . import package
from . import qcmodule
from . import errors
from . import install

log = util.logger(__name__)


class BuildInfo(object):
    def __init__(self, repo,
                    qcc_cmd='rmqcc',
                    output_dir=None,
                    qcc_flags=None,
                    comment="custom build",
                    suffix=None,
                    autocvars='compatible',
                    threads=8,
                    extra_packages=(),
                    link_pk3dirs=False,
                    compress_gfx=True,
                    compress_gfx_quality=85,
                    cache_dir=None,
                    cache_qc=True,
                    cache_pkg=True,
                    force_rebuild=False,
                    hooks=None,
                ):

        if hooks is None:
            hooks = {}

        self.__dict__.update(locals())

        self.date = datetime.datetime.now()
        self.date_string = self.date.strftime('%F %T %Z').strip()

        if suffix is None:
            suffix = repo.rm_branch
            if suffix == 'master':
                suffix = ''

        self.name = "RocketMinsta"

        if suffix:
            self.name += '-' + suffix

        self.suffix = suffix
        self.version = repo.rm_version

        if cache_dir is not None:
            self.cache_dir = util.make_directory(cache_dir).resolve()
        else:
            self.cache_dir = None

        self.temp_dir = util.temp_directory()

        if output_dir is None:
            output_dir = self.temp_dir / 'build'

        self.output_dir = util.make_directory(output_dir).resolve()

        if qcc_flags is None:
            qcc_flags = []
        elif isinstance(qcc_flags, str):
            qcc_flags = shlex.split(qcc_flags)

        self.qcc_flags = qcc_flags

        self.qc_defs = self.get_qc_defs()
        self.qc_module_config = {}
        self.configure_qc_modules()

        self.built_qc_modules = {}
        self.built_packages = []

        self.install = functools.partial(install.install, self)

    def configure_qc_module(self, name, *args, **kwargs):
        if name in self.qc_module_config:
            cfgs = self.qc_module_config[name]
        else:
            cfgs = []
            self.qc_module_config[name] = cfgs

        cfgs.append(qcmodule.BuildConfig(*args, **kwargs))

    def configure_qc_modules(self):
        extraflags = {
            module: [] for module in self.repo.qc_modules
        }

        flags_configdefs = ['-DRM_NO_AUTO_HEADER'] + [
            ('-D%s=%s' % (key, value)) if value else ('-D%s' % key)
                for key, value in self.qc_defs.items()
        ]

        flags_autocvars = ['-DRM_AUTOCVARS']

        for name, module in self.repo.qc_modules.items():
            if not module.needs_auto_header:
                extraflags[name] += flags_configdefs

        if self.autocvars == 'enable':
            for module in self.repo.qc_modules:
                extraflags[module] += flags_autocvars
        elif self.autocvars == 'compatible':
            extraflags['server'] += flags_autocvars

            self.configure_qc_module(
                'client',
                qcc_cmd=self.qcc_cmd,
                qcc_flags=self.qcc_flags + flags_autocvars + extraflags['client'],
                dat_expected_name='csprogs',
                dat_final_name='rocketminsta_cl_autocvars',
                cvar='csqc_progname_alt',
            )
        elif self.autocvars != 'disable':
            raise ValueError(
                "'autocvars' must be one of: 'enable', 'disable', 'compatible'; got %r instead" % self.autocvars
            )

        self.configure_qc_module(
            'server',
            qcc_cmd=self.qcc_cmd,
            qcc_flags=self.qcc_flags + extraflags['server'],
            dat_expected_name='progs',
            dat_final_name='rocketminsta_sv',
            cvar='sv_progs',
        )

        self.configure_qc_module(
            'client',
            qcc_cmd=self.qcc_cmd,
            qcc_flags=self.qcc_flags + extraflags['client'],
            dat_expected_name='csprogs',
            dat_final_name='rocketminsta_cl',
            cvar='csqc_progname',
        )

        self.configure_qc_module(
            'menu',
            qcc_cmd=self.qcc_cmd,
            qcc_flags=self.qcc_flags + extraflags['menu'],
            dat_expected_name='menu',
            dat_final_name='menu',
        )

    def should_install_qc_module(self, name):
        return name != 'menu'

    def should_build_package(self, pkg):
        return (not (
            pkg.name.startswith('c_') or
            pkg.name.startswith('o_')
        )) or pkg.name in self.extra_packages

    def call_hook(self, hook, **kwargs):
        if hook not in self.hooks:
            return

        log.debug('Calling hook %r (keywords=%r)', hook, kwargs)
        return self.hooks[hook](
            build_info=self,
            log=util.logger(__name__, 'hook', hook),
            **kwargs
        )

    def get_qc_defs(self):
        defs = {
            'RM_BUILD_DATE': '"%s (%s)"' % (self.date_string, self.comment),
            'RM_BUILD_NAME': '"%s"' % (self.name),
            'RM_BUILD_VERSION': '"%s"' % self.version,
            'RM_BUILD_MENUSUM': '"%s"' % self.repo.qchash_menu.hexdigest(),
            'RM_BUILD_SUFFIX': '"%s"' % self.suffix,
        }

        for name, pkg in self.repo.packages.items():
            if self.should_build_package(pkg):
                defs['RM_SUPPORT_PKG_%s' % name] = None

        return defs


class Repo(object):
    MAX_VERSION = 0

    def __init__(self, path):
        self.version = 0
        self.packages = {}
        self.qc_modules = {}
        self.root = path
        self.qchash_common = None
        self.qchash_menu = None

    @property
    def root(self):
        return self._root

    @root.setter
    def root(self, path):
        self.init_paths(path)

    @property
    def qcsrc(self):
        return self._qcsrc

    @property
    def modfiles(self):
        return self._modfiles

    def init_paths(self, path):
        self._root = util.directory(path).resolve()

        try:
            with (self._root / '.rmbuild_repoversion').open() as f:
                self.version = int(f.read().strip())
        except FileNotFoundError:
            self.version = 0

        if self.version > self.MAX_VERSION:
            raise errors.VersionError('Repo version is %i, maxiumum supported is %i. Please update rmbuild.' %
                                      (self.version, self.MAX_VERSION))

        self._qcsrc = util.directory(self._root / 'qcsrc')
        self._modfiles = util.directory(self._root / 'modfiles')

        with util.in_dir(self.root):
            self.rm_branch = util.git('rev-parse', '--abbrev-ref', 'HEAD')
            self.rm_version = util.git('describe', '--tags', '--long', '--dirty')

        self.init_packages()
        self.init_qc_modules()

    def init_packages(self):
        for pdir in self.root.glob('*.pk3dir'):
            self.packages[pdir.stem] = package.construct(pdir.stem, pdir)

    def init_qc_modules(self):
        for name in ('server', 'client', 'menu'):
            self.qc_modules[name] = qcmodule.QCModule(name, self.qcsrc / name)

    def build(self, *buildinfo_args, **buildinfo_kwargs):
        self.update_qcsrc_hashes()
        build_info = BuildInfo(self, *buildinfo_args, **buildinfo_kwargs)
        log.info("Build started: %s %s (%s)", build_info.name, self.rm_version, build_info.comment)

        util.clear_directory(build_info.output_dir)

        auto_header_needed = False
        for qc in self.qc_modules.values():
            if qc.needs_auto_header:
                auto_header_needed = True
                break

        with util.in_dir(build_info.temp_dir):
            if auto_header_needed:
                self.generate_qc_header(build_info)

            w = util.Worker('AsyncBuilder', threads=build_info.threads)
            self.build_qc_modules_async(build_info, w)
            self.build_packages_async(build_info, w)
            w.start()
            w.wait()

            if w.errors:
                raise errors.RMBuildError("Errors occured during asynchronous operations")

            self.post_build_packages(build_info)
            self.install_qc_modules(build_info)
            self.copy_static_files(build_info)
            self.update_rm_cfg(build_info)

        delta = datetime.datetime.now() - build_info.date

        log.info(
            "Build finished: %s %s (%s), target: %r, build time: %s",
            build_info.name,
            self.rm_version,
            build_info.comment,
            str(build_info.output_dir),
            delta
        )

        build_info.call_hook('post_build')
        return build_info

    def update_qcsrc_hashes(self):
        log.info("Hashing the QC source files")

        chash = util.hash_constructor()
        util.hash_path(self.qcsrc / 'common', hashobject=chash, namefilter=util.namefilter_qcmodule)

        mhash = chash.copy()
        util.hash_path(self.qcsrc / 'menu', hashobject=mhash, namefilter=util.namefilter_qcmodule)

        util.hash_path(self.qcsrc / 'warpzonelib', hashobject=chash, namefilter=util.namefilter_qcmodule)

        self.qchash_common = chash
        self.qchash_menu = mhash

    def generate_qc_header(self, build_info):
        log.info("Generating the rm_auto header")

        with open(str(self.qcsrc / 'common' / 'rm_auto.qh'), 'w') as header:
            for key, value in build_info.qc_defs.items():
                if value:
                    header.write('#define %s %s\n' % (key, value))
                else:
                    header.write('#define %s\n' % key)

    def build_packages_async(self, build_info, worker):
        for name, pkg in self.packages.items():
            if not build_info.should_build_package(pkg):
                continue

            @worker.add_task
            def task(name=name, pkg=pkg, build_info=build_info):
                log.debug('build() for %s', name)
                pkg.build(build_info)
                build_info.built_packages.append(pkg)

    def post_build_packages(self, build_info):
        log.info("Building special client-side packages")

        for name, pkg in self.packages.items():
            if not build_info.should_build_package(pkg):
                continue

            log.debug('post_build() for %s', name)
            pkg.post_build(build_info)

    def build_qc_modules_async(self, build_info, worker):
        built = build_info.built_qc_modules

        for name, module in self.qc_modules.items():
            built[name] = []
            for config in build_info.qc_module_config[name]:

                @worker.add_task
                def task(name=name, built=built, module=module, build_info=build_info, config=config):
                    built[name].append(module.build(build_info, config))

    def install_qc_module(self, build_info, built_module):
        for fpath in filter(lambda p: p.suffix in util.QC_INSTALL_FILEEXT, built_module.iterdir()):
            util.copy(fpath, build_info.output_dir)

    def install_qc_modules(self, build_info):
        log.info("Installing QC modules")

        for name, dirs in build_info.built_qc_modules.items():
            if not build_info.should_install_qc_module(name):
                continue

            for module in dirs:
                self.install_qc_module(build_info, module)

    def copy_static_files(self, build_info):
        log.info("Copying static files")
        util.copy_tree(self.modfiles, build_info.output_dir)

    def update_rm_cfg(self, build_info):
        log.info("Updating rocketminsta.cfg")

        with (build_info.output_dir / 'rocketminsta.cfg').open('a') as rmcfg:
            rmcfg.write('\n\n// The rest of this file was autogenerated by rmbuild\n\n')
            rmcfg.write('rm_clearpkgs\n')

            for pkg in build_info.built_packages:
                rmcfg.write('rm_putpackage %s\n' % pkg.metafile_name)

            rmcfg.write('\n')

            for name, cfgs in build_info.qc_module_config.items():
                for cfg in cfgs:
                    if cfg.cvar:
                        rmcfg.write('set %s %s.dat\n' % (cfg.cvar, cfg.dat_final_name))

            rmcfg.write('\n')

    def __repr__(self):
        return 'Repo(%r)' % str(self._root)
