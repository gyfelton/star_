#!/usr/bin/env python

"""Build tool that finds dependencies automatically for any language.

fabricate is a build tool that finds dependencies automatically for any
language. It's small and just works. No hidden stuff behind your back. It was
inspired by Bill McCloskey's make replacement, memoize, but fabricate works on
Windows as well as Linux.

Read more about how to use it and how it works on the project page:
    http://code.google.com/p/fabricate/

Like memoize, fabricate is released under a "New BSD license". fabricate is
copyright (c) 2009 Brush Technology. Full text of the license is here:
    http://code.google.com/p/fabricate/wiki/License

To get help on fabricate functions:
    from fabricate import *
    help(function)

"""

# fabricate version number
__version__ = '1.14'

# if version of .deps file has changed, we know to not use it
deps_version = 2

import atexit
import optparse
import os
import platform
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

try:
    WindowsError
except:
    class WindowsError(Exception): pass

# so you can do "from fabricate import *" to simplify your build script
__all__ = ['setup', 'run', 'run_multiple', 'autoclean', 'main', 'shell', 'fabricate_version',
           'memoize', 'outofdate', 
           'ExecutionError', 'md5_hasher', 'mtime_hasher',
           'Runner', 'AtimesRunner', 'StraceRunner', 'AlwaysRunner',
           'SmartRunner', 'Builder']

import textwrap

__doc__ += "Exported functions are:\n" + '  ' + '\n  '.join(textwrap.wrap(', '.join(__all__), 80))



FAT_atime_resolution = 24*60*60     # resolution on FAT filesystems (seconds)
FAT_mtime_resolution = 2

# NTFS resolution is < 1 ms
# We assume this is considerably more than time to run a new process

NTFS_atime_resolution = 0.0002048   # resolution on NTFS filesystems (seconds)
NTFS_mtime_resolution = 0.0002048   #  is actually 0.1us but python's can be
                                    #  as low as 204.8us due to poor
                                    #  float precision when storing numbers
                                    #  as big as NTFS file times can be
                                    #  (float has 52-bit precision and NTFS
                                    #  FILETIME has 63-bit precision, so
                                    #  we've lost 11 bits = 2048)

# So we can use md5func in old and new versions of Python without warnings
try:
    import hashlib
    md5func = hashlib.md5
except ImportError:
    import md5
    md5func = md5.new

# Use json, or pickle on older Python versions if simplejson not installed
try:
    import json
except ImportError:
    try:
        import simplejson as json
    except ImportError:
        import cPickle
        # needed to ignore the indent= argument for pickle's dump()
        class PickleJson:
            def load(self, f):
                return cPickle.load(f)
            def dump(self, obj, f, indent=None, sort_keys=None):
                return cPickle.dump(obj, f)
        json = PickleJson()

def printerr(message):
    """ Print given message to stderr with a line feed. """
    print >>sys.stderr, message

class PathError(Exception): pass

class ExecutionError(Exception):
    """ Raised by shell() and run() if command returns non-zero exit code. """
    pass

def args_to_list(args):
    """ Return a flat list of the given arguments for shell(). """
    arglist = []
    for arg in args:
        if arg is None:
            continue
        if hasattr(arg, '__iter__'):
            arglist.extend(args_to_list(arg))
        else:
            if not isinstance(arg, basestring):
                arg = str(arg)
            arglist.append(arg)
    return arglist

def shell(*args, **kwargs):
    r""" Run a command: program name is given in first arg and command line
        arguments in the rest of the args. Iterables (lists and tuples) in args
        are recursively converted to separate arguments, non-string types are
        converted with str(arg), and None is ignored. For example:

        >>> def tail(input, n=3, flags=None):
        >>>     args = ['-n', n]
        >>>     return shell('tail', args, flags, input=input)
        >>> tail('a\nb\nc\nd\ne\n')
        'c\nd\ne\n'
        >>> tail('a\nb\nc\nd\ne\n', 2, ['-v'])
        '==> standard input <==\nd\ne\n'

        Keyword arguments kwargs are interpreted as follows:

        "input" is a string to pass standard input into the process (or the
            default of None to use parent's stdin, eg: the keyboard)
        "silent" is True (default) to return process's standard output as a
            string, or False to print it as it comes out
        "shell" set to True will run the command via the shell (/bin/sh or
            COMSPEC) instead of running the command directly (the default)

        Raises ExecutionError(message, output, status) if the command returns
        a non-zero status code. """
    return _shell(args, **kwargs)

def _shell(args, input=None, silent=True, shell=False):
    if input:
        stdin = subprocess.PIPE
    else:
        stdin = None
    if silent:
        stdout = subprocess.PIPE
    else:
        stdout = None
    arglist = args_to_list(args)
    if not arglist:
        raise TypeError('shell() takes at least 1 argument (0 given)')
    if shell:
        # handle subprocess.Popen quirk where subsequent args are passed
        # to bash instead of to our command
        command = subprocess.list2cmdline(arglist)
    else:
        command = arglist
    try:
        proc = subprocess.Popen(command, stdin=stdin, stdout=stdout,
                                stderr=subprocess.STDOUT, shell=shell)
    except WindowsError, e:
        # Work around the problem that windows Popen doesn't say what file it couldn't find
        if e.errno==2 and e.filename==None:
            e.filename = arglist[0]
        raise e
    output, stderr = proc.communicate(input)
    status = proc.wait()
    if status:
        raise ExecutionError('%r exited with status %d'
                             % (os.path.basename(arglist[0]), status),
                             output, status)
    if silent:
        return output

def md5_hasher(filename):
    """ Return MD5 hash of given filename, or None if file doesn't exist. """
    try:
        f = open(filename, 'rb')
        try:
            return md5func(f.read()).hexdigest()
        finally:
            f.close()
    except IOError:
        return None

def mtime_hasher(filename):
    """ Return modification time of file, or None if file doesn't exist. """
    try:
        st = os.stat(filename)
        return repr(st.st_mtime)
    except (IOError, OSError):
        return None

class RunnerUnsupportedException(Exception):
    """ Exception raise by Runner constructor if it is not supported
        on the current platform."""
    pass

class Runner(object):
    def __call__(self, *args, **kwargs):
        """ Run command and return (dependencies, outputs), where
            dependencies is a list of the filenames of files that the
            command depended on, and output is a list of the filenames
            of files that the command modified. The input is passed
            to shell()"""
        raise NotImplementedError("Runner subclass called but subclass didn't define __call__")
    def ignore(self, name):
        return self._builder.ignore.search(name)



class AtimesRunner(Runner):
    def __init__(self, builder):
        self._builder = builder
        self.atimes = AtimesRunner.has_atimes(self._builder.dirs)
        if self.atimes == 0:
            raise RunnerUnsupportedException(
                'atimes are not supported on this platform')

    @staticmethod
    def file_has_atimes(filename):
        """ Return whether the given filesystem supports access time updates for
            this file. Return:
              - 0 if no a/mtimes not updated
              - 1 if the atime resolution is at least one day and
                the mtime resolution at least 2 seconds (as on FAT filesystems)
              - 2 if the atime and mtime resolutions are both < ms
                (NTFS filesystem has 100 ns resolution). """

        def access_file(filename):
            """ Access (read a byte from) file to try to update its access time. """
            f = open(filename)
            f.read(1)
            f.close()

        initial = os.stat(filename)
        os.utime(filename, (
            initial.st_atime-FAT_atime_resolution,
            initial.st_mtime-FAT_mtime_resolution))

        adjusted = os.stat(filename)
        access_file(filename)
        after = os.stat(filename)

        # Check that a/mtimes actually moved back by at least resolution and
        #  updated by a file access.
        #  add NTFS_atime_resolution to account for float resolution factors
        #  Comment on resolution/2 in atimes_runner()
        if initial.st_atime-adjusted.st_atime > FAT_atime_resolution+NTFS_atime_resolution or \
           initial.st_mtime-adjusted.st_mtime > FAT_mtime_resolution+NTFS_atime_resolution or \
           initial.st_atime==adjusted.st_atime or \
           initial.st_mtime==adjusted.st_mtime or \
           not after.st_atime-FAT_atime_resolution/2 > adjusted.st_atime:
            return 0

        os.utime(filename, (
            initial.st_atime-NTFS_atime_resolution,
            initial.st_mtime-NTFS_mtime_resolution))
        adjusted = os.stat(filename)

        # Check that a/mtimes actually moved back by at least resolution
        # Note: != comparison here fails due to float rounding error
        #  double NTFS_atime_resolution to account for float resolution factors
        if initial.st_atime-adjusted.st_atime > NTFS_atime_resolution*2 or \
           initial.st_mtime-adjusted.st_mtime > NTFS_mtime_resolution*2 or \
           initial.st_atime==adjusted.st_atime or \
           initial.st_mtime==adjusted.st_mtime:
            return 1

        return 2

    @staticmethod
    def exists(path):
        if not os.path.exists(path):
            # Note: in linux, error may not occur: strace runner doesn't check
            raise PathError("build dirs specified a non-existant path '%s'" % path)

    @staticmethod
    def has_atimes(paths):
        """ Return whether a file created in each path supports atimes and mtimes.
            Return value is the same as used by file_has_atimes
            Note: for speed, this only tests files created at the top directory
            of each path. A safe assumption in most build environments.
            In the unusual case that any sub-directories are mounted
            on alternate file systems that don't support atimes, the build may
            fail to identify a dependency """

        atimes = 2                  # start by assuming we have best atimes
        for path in paths:
            AtimesRunner.exists(path)
            handle, filename = tempfile.mkstemp(dir=path)
            try:
                try:
                    f = os.fdopen(handle, 'wb')
                except:
                    os.close(handle)
                    raise
                try:
                    f.write('x')    # need a byte in the file for access test
                finally:
                    f.close()
                atimes = min(atimes, AtimesRunner.file_has_atimes(filename))
            finally:
                os.remove(filename)
        return atimes

    def _file_times(self, path, depth):
        """ Helper function for file_times().
            Return a dict of file times, recursing directories that don't
            start with self._builder.ignoreprefix """

        AtimesRunner.exists(path)
        names = os.listdir(path)
        times = {}
        ignoreprefix = self._builder.ignoreprefix
        for name in names:
            if ignoreprefix and name.startswith(ignoreprefix):
                continue
            if path == '.':
                fullname = name
            else:
                fullname = os.path.join(path, name)
            st = os.stat(fullname)
            if stat.S_ISDIR(st.st_mode):
                if depth > 1:
                    times.update(self._file_times(fullname, depth-1))
            elif stat.S_ISREG(st.st_mode):
                times[fullname] = st.st_atime, st.st_mtime
        return times

    def file_times(self):
        """ Return a dict of "filepath: (atime, mtime)" entries for each file
            in self._builder.dirs. "filepath" is the absolute path, "atime" is
            the access time, "mtime" the modification time.
            Recurse directories that don't start with
            self._builder.ignoreprefix and have depth less than
            self._builder.dirdepth. """

        times = {}
        for path in self._builder.dirs:
            times.update(self._file_times(path, self._builder.dirdepth))
        return times

    def _utime(self, filename, atime, mtime):
        """ Call os.utime but ignore permission errors """
        try:
            os.utime(filename, (atime, mtime))
        except OSError, e:
            # ignore permission errors -- we can't build with files
            # that we can't access anyway
            if e.errno != 1:
                raise

    def _age_atimes(self, filetimes):
        """ Age files' atimes and mtimes to be at least FAT_xx_resolution old.
            Only adjust if the given filetimes dict says it isn't that old,
            and return a new dict of filetimes with the ages adjusted. """
        adjusted = {}
        now = time.time()
        for filename, entry in filetimes.iteritems():
            if now-entry[0] < FAT_atime_resolution or now-entry[1] < FAT_mtime_resolution:
                entry = entry[0] - FAT_atime_resolution, entry[1] - FAT_mtime_resolution
                self._utime(filename, entry[0], entry[1])
            adjusted[filename] = entry
        return adjusted

    def __call__(self, *args, **kwargs):
        """ Run command and return its dependencies and outputs, using before
            and after access times to determine dependencies. """

        # For Python pre-2.5, ensure os.stat() returns float atimes
        old_stat_float = os.stat_float_times()
        os.stat_float_times(True)

        originals = self.file_times()
        if self.atimes == 2:
            befores = originals
            atime_resolution = 0
            mtime_resolution = 0
        else:
            befores = self._age_atimes(originals)
            atime_resolution = FAT_atime_resolution
            mtime_resolution = FAT_mtime_resolution
        shell_keywords = dict(silent=False)
        shell_keywords.update(kwargs)
        shell(*args, **shell_keywords)
        afters = self.file_times()
        deps = set()
        outputs = set()
        for name in afters:
            if name in befores:
                # if file exists before+after && mtime changed, add to outputs
                # Note: Can't just check that atimes > than we think they were
                #       before because os might have rounded them to a later
                #       date than what we think we set them to in befores.
                #       So we make sure they're > by at least 1/2 the
                #       resolution.  This will work for anything with a
                #       resolution better than FAT.
                if afters[name][1]-mtime_resolution/2 > befores[name][1]:
                    outputs.add(name)
                elif afters[name][0]-atime_resolution/2 > befores[name][0]:
                    # otherwise add to deps if atime changed
                    if not self.ignore(name):
                        deps.add(name)
            else:
                # file created (in afters but not befores), add as output
                if not self.ignore(name):
                    outputs.add(name)

        if self.atimes < 2:
            # Restore atimes of files we didn't access: not for any functional
            # reason -- it's just to preserve the access time for the user's info
            for name in deps:
                originals.pop(name)
            for name in originals:
                original = originals[name]
                if original != afters.get(name, None):
                    self._utime(name, original[0], original[1])

        os.stat_float_times(old_stat_float)  # restore stat_float_times value
        return deps, outputs

class StraceProcess(object):
    def __init__(self, cwd='.'):
        self.cwd = cwd
        self.deps = set()
        self.outputs = set()

    def add_dep(self, dep):
        self.deps.add(dep)

    def add_output(self, output):
        self.outputs.add(output)

    def __str__(self):
        return '<StraceProcess cwd=%s deps=%s outputs=%s>' % \
               (self.cwd, self.deps, self.outputs)

class StraceRunner(Runner):
    keep_temps = False

    def __init__(self, builder):
        self.strace_version = StraceRunner.get_strace_version()
        if self.strace_version == 0:
            raise RunnerUnsupportedException('strace is not available')
        if self.strace_version == 32:
            self._stat_re = self._stat32_re
            self._stat_func = 'stat'
        else:
            self._stat_re = self._stat64_re
            self._stat_func = 'stat64'
        self._builder = builder
        self.temp_count = 0

    @staticmethod
    def get_strace_version():
        """ Return 0 if this system doesn't have strace, nonzero otherwise
            (64 if strace supports stat64, 32 otherwise). """
        if platform.system() == 'Windows':
            # even if windows has strace, it's probably a dodgy cygwin one
            return 0
        try:
            proc = subprocess.Popen(['strace', '-e', 'trace=stat64'], stderr=subprocess.PIPE)
            stdout, stderr = proc.communicate()
            proc.wait()
            if 'invalid system call' in stderr:
                return 32
            else:
                return 64
        except OSError:
            return 0

    # Regular expressions for parsing of strace log
    _open_re       = re.compile(r'(?P<pid>\d+)\s+open\("(?P<name>[^"]*)", (?P<mode>[^,)]*)')
    _stat32_re     = re.compile(r'(?P<pid>\d+)\s+stat\("(?P<name>[^"]*)", .*')
    _stat64_re     = re.compile(r'(?P<pid>\d+)\s+stat64\("(?P<name>[^"]*)", .*')
    _execve_re     = re.compile(r'(?P<pid>\d+)\s+execve\("(?P<name>[^"]*)", .*')
    _mkdir_re      = re.compile(r'(?P<pid>\d+)\s+mkdir\("(?P<name>[^"]*)", .*')
    _rename_re     = re.compile(r'(?P<pid>\d+)\s+rename\("[^"]*", "(?P<name>[^"]*)"\)')
    _kill_re       = re.compile(r'(?P<pid>\d+)\s+killed by.*')
    _chdir_re      = re.compile(r'(?P<pid>\d+)\s+chdir\("(?P<cwd>[^"]*)"\)')
    _exit_group_re = re.compile(r'(?P<pid>\d+)\s+exit_group\((?P<status>.*)\).*')
    _clone_re      = re.compile(r'(?P<pid_clone>\d+)\s+(clone|fork|vfork)\(.*\)\s*=\s*(?P<pid>\d*)')

    # Regular expressions for detecting interrupted lines in strace log
    # 3618  clone( <unfinished ...>
    # 3618  <... clone resumed> child_stack=0, flags=CLONE, child_tidptr=0x7f83deffa780) = 3622
    _unfinished_start_re = re.compile(r'(?P<pid>\d+)(?P<body>.*)<unfinished ...>$')
    _unfinished_end_re   = re.compile(r'(?P<pid>\d+)\s+\<\.\.\..*\>(?P<body>.*)')

    def _do_strace(self, args, kwargs, outfile, outname):
        """ Run strace on given command args/kwargs, sending output to file.
            Return (status code, list of dependencies, list of outputs). """
        shell_keywords = dict(silent=False)
        shell_keywords.update(kwargs)
        shell('strace', '-fo', outname, '-e',
              'trace=open,%s,execve,exit_group,chdir,mkdir,rename,clone,vfork,fork' % self._stat_func,
              args, **shell_keywords)
        cwd = '.' 
        status = 0
        processes  = {}  # dictionary of processes (key = pid)
        unfinished = {}  # list of interrupted entries in strace log
        for line in outfile:
            # look for split lines
            unfinished_start_match = self._unfinished_start_re.match(line)
            unfinished_end_match = self._unfinished_end_re.match(line)
            if unfinished_start_match:
                pid = unfinished_start_match.group('pid')
                body = unfinished_start_match.group('body')
                unfinished[pid] = pid + ' ' + body
                continue
            elif unfinished_end_match:
                pid = unfinished_end_match.group('pid')
                body = unfinished_end_match.group('body')
                line = unfinished[pid] + body
                del unfinished[pid]

            is_output = False
            open_match = self._open_re.match(line)
            stat_match = self._stat_re.match(line)
            execve_match = self._execve_re.match(line)
            mkdir_match = self._mkdir_re.match(line)
            rename_match = self._rename_re.match(line)
            clone_match = self._clone_re.match(line)  

            kill_match = self._kill_re.match(line)
            if kill_match:
                return None, None, None

            match = None
            if execve_match:
                pid = execve_match.group('pid')
                if pid not in processes:
                    processes[pid] = StraceProcess()
                    match = execve_match
            elif clone_match:
                pid = clone_match.group('pid')
                pid_clone = clone_match.group('pid_clone')
                processes[pid] = StraceProcess(processes[pid_clone].cwd)
            elif open_match:
                match = open_match
                mode = match.group('mode')
                if 'O_WRONLY' in mode or 'O_RDWR' in mode:
                    # it's an output file if opened for writing
                    is_output = True
            elif stat_match:
                match = stat_match
            elif mkdir_match:
                match = mkdir_match                
            elif rename_match:
                match = rename_match
                # the destination of a rename is an output file
                is_output = True
                
            if match:
                name = match.group('name')
                pid  = match.group('pid')
                cwd = processes[pid].cwd
                if cwd != '.':
                    name = os.path.join(cwd, name)

                if (self._builder._is_relevant(name)
                    and not self.ignore(name)
                    and (os.path.isfile(name)
                         or os.path.isdir(name)
                         or not os.path.lexists(name))):
                    if is_output:
                        processes[pid].add_output(name)
                    else:
                        processes[pid].add_dep(name)

            match = self._chdir_re.match(line)
            if match:
                processes[pid].cwd = os.path.join(processes[pid].cwd, match.group('cwd'))

            match = self._exit_group_re.match(line)
            if match:
                status = int(match.group('status'))

        # collect outputs and dependencies from all processes
        deps = set()
        outputs = set()
        for pid, process in processes.items():
            deps = deps.union(process.deps)
            outputs = outputs.union(process.outputs)

        return status, deps, outputs

    def __call__(self, *args, **kwargs):
        """ Run command and return its dependencies and outputs, using strace
            to determine dependencies (by looking at what files are opened or
            modified). """
        if self.keep_temps:
            outname = 'strace%03d.txt' % self.temp_count
            self.temp_count += 1
            handle = os.open(outname, os.O_CREAT)
        else:
            handle, outname = tempfile.mkstemp()

        try:
            try:
                outfile = os.fdopen(handle, 'r')
            except:
                os.close(handle)
                raise
            try:
                status, deps, outputs = self._do_strace(args, kwargs, outfile, outname)
                if status is None:
                    raise ExecutionError(
                        '%r was killed unexpectedly' % args[0], '', -1)
            finally:
                outfile.close()
        finally:
            if not self.keep_temps:
                os.remove(outname)

        if status:
            raise ExecutionError('%r exited with status %d'
                                 % (os.path.basename(args[0]), status),
                                 '', status)
        return deps, outputs

class InterposingRunner(Runner):
    keep_temps = False

    train = '''
#include <fcntl.h>
#include <sys/stat.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
#include <sys/param.h>
#include <mach-o/dyld.h>
#include <unistd.h>
// from dyld-interposing.h
#define DYLD_INTERPOSE(_replacement,_replacee)    __attribute__((used)) static struct{ const void* replacement; const void* replacee; } _interpose_##_replacee             __attribute__ ((section ("__DATA,__interpose"))) = { (const void*)(unsigned long)&_replacement, (const void*)(unsigned long)&_replacee };

static int outf = -1;
// sometimes files are opened very early in a fork - in this case, fprintf doesn't work
#define output(fmt, args...) do { char obuf[1024]; write(outf, obuf, snprintf(obuf, 1024, fmt, ##args)); } while(0)

static void record(const char *func, const char *path, bool is_write) {
    if(!path || (path[0] == '.' && !path[1])) return;
    if(path[0] == '/') {
        output("!%s.%s %s\\n", is_write ? "write" : "read", func, path);
    } else {
        char wd[MAXPATHLEN]; getwd(wd);
        output("!%s.%s %s/%s\\n", is_write ? "write" : "read", func, wd, path);
    }
}

__attribute__((constructor))
static void init() {
    outf = open(getenv("TRAIN_OUTPUT"), O_WRONLY | O_CREAT | O_APPEND, 0755);
    if(outf == -1) {
        char *error = "Could not open TRAIN_OUTPUT\\n";
        write(2, error, strlen(error));
        abort();
    }
    char buf[PATH_MAX+1];
    uint32_t bufsize = PATH_MAX;
    if(!_NSGetExecutablePath(buf, &bufsize)) {
        buf[bufsize] = 0;
        record("init", buf, false);
    }
}

#define do_generic(rettype, name, args1, args2, write) \\
rettype orig_##name args1 __asm__("_" #name); \\
rettype my_##name args1 { \\
    rettype ret = orig_##name args2; \\
    record(#name, path, write); \\
    return ret; \\
} \\
DYLD_INTERPOSE(my_##name, orig_##name)

#define do_open(name) do_generic(int, name, (const char *path, int oflag, int mode), (path, oflag, mode), oflag & (O_WRONLY | O_RDWR))

#ifdef __LP64__
do_open(open)
do_open(open$NOCANCEL)
#else
do_open(open)
do_open(open$UNIX2003)
do_open(open$NOCANCEL$UNIX2003)
#endif

// dyld uses its own version of open() et al; this isn't perfect but suffices
do_generic(void *, dlopen, (const char *path, int mode), (path, mode), false)
do_generic(const struct mach_header *, NSAddImage, (const char *path, uint32_t options), (path, options), false)
do_generic(bool, NSAddLibraryWithSearching, (const char *path), (path), false)
do_generic(bool, NSAddLibrary, (const char *path), (path), false)
do_generic(NSObjectFileImageReturnCode, NSCreateObjectFileImageFromFile, (const char *path, NSObjectFileImage *objectFileImage), (path, objectFileImage), false)
do_generic(NSModule, NSLinkModule, (NSObjectFileImage objectFileImage, const char *path, uint32_t options), (objectFileImage, path, options), false)

#define do_stat(name) do_generic(int, name, (const char *path, struct stat *st), (path, st), false)
do_stat(stat)
do_stat(stat$INODE64)
do_stat(lstat)
do_stat(lstat$INODE64)

do_generic(int, mkdir, (const char *path, mode_t mode), (path, mode), true)

int my_rename(const char *old, const char *new) {
    record("rename", old, true); 
    int ret = rename(old, new);
    record("rename", new, true);
    return ret;
}
DYLD_INTERPOSE(my_rename, rename)

int my_unlink(const char *path) {
    record("unlink", path, true);
    return unlink(path);
}
DYLD_INTERPOSE(my_unlink, unlink)
'''
    def __init__(self, builder):
        self._builder = builder
        self.temp_count = 0
    
    def __call__(self, *args):
        dylib_path = os.path.expanduser('~/.fabricate.dylib')
        c_path = os.path.expanduser('~/.fabricate.c')
        if not (os.path.exists(dylib_path) and os.path.exists(c_path) and open(c_path).read() == self.train):
            open(c_path, 'w').write(self.train)
            try:
                shell('gcc', '-arch', 'i386', '-arch', 'x86_64', '-dynamiclib', '-Wall', '-Werror', '-Wno-deprecated-declarations', '-o', dylib_path, c_path, silent=False)
            except ExecutionError:
                raise
        
        if self.keep_temps:
            outname = 'interposing%03d.txt' % self.temp_count
            self.temp_count += 1
            handle = os.open(outname, os.O_CREAT)
        else:
            handle, outname = tempfile.mkstemp()

        os.environ['DYLD_INSERT_LIBRARIES'] = dylib_path
        os.environ['TRAIN_OUTPUT'] = outname
        open(outname, 'w').close()

        try:
            try:
                outfile = os.fdopen(handle, 'r')
            except:
                os.close(handle)
                raise
            try:
                output = shell(*args, silent=False)

                deps = set()
                outputs = set()
                for line in outfile:
                    assert line.startswith('!') 
                    read_write = line[1:line.find('.')]
                    name = line[line.find(' ')+1:-1]

                    bits = []
                    for bit in name.split('/'):
                        if bit == '..':
                            if len(bits) > 1: bits.pop()
                        else:
                            bits.append(bit)

                    name = '/'.join(bits)
                
                    if (self._builder._is_relevant(name)
                        and not self.ignore(name)
                        and (os.path.isfile(name)
                            or os.path.isdir(name)
                            or not os.path.lexists(name))):

                        if read_write == 'read':
                            deps.add(name)
                        elif read_write == 'write':
                            outputs.add(name)
                        else:
                            raise ValueError(line)
            finally:
                outfile.close()
        finally:
            if not self.keep_temps:
                os.remove(outname)
        
        return deps, outputs
        

class AlwaysRunner(Runner):
    def __init__(self, builder):
        pass

    def __call__(self, *args, **kwargs):
        """ Runner that always runs given command, used as a backup in case
            a system doesn't have strace or atimes. """
        shell_keywords = dict(silent=False)
        shell_keywords.update(kwargs)
        shell(*args, **shell_keywords)
        return None, None

class SmartRunner(Runner):
    def __init__(self, builder):
        self._builder = builder
        self._runner = None

    def __call__(self, *args, **kwargs):
        """ Smart command runner that uses StraceRunner if it can,
            otherwise AtimesRunner if available, otherwise AlwaysRunner.
            When first called, it caches which runner it used for next time."""

        if self._runner is None:
            try:
                if os.path.exists('/usr/lib/dyld'):
                    self._runner = InterposingRunner(self._builder)
                else:
                    self._runner = StraceRunner(self._builder)
            except RunnerUnsupportedException:
                try:
                    self._runner = AtimesRunner(self._builder)
                except RunnerUnsupportedException:
                    self._runner = AlwaysRunner(self._builder)

        return self._runner(*args, **kwargs)

class Builder(object):
    """ The Builder.

        You may supply a "runner" class to change the way commands are run
        or dependencies are determined. For an example, see:
            http://code.google.com/p/fabricate/wiki/HowtoMakeYourOwnRunner

        A "runner" must be a subclass of Runner and must have a __call__()
        function that takes a command as a list of args and returns a tuple of
        (deps, outputs), where deps is a list of rel-path'd dependency files
        and outputs is a list of rel-path'd output files. The default runner
        is SmartRunner, which automatically picks one of StraceRunner,
        AtimesRunner, or AlwaysRunner depending on your system.
        A "runner" class may have an __init__() function that takes the
        builder as a parameter.
    """

    def __init__(self, runner=None, dirs=None, dirdepth=100, ignoreprefix='.',
                 ignore=None, hasher=md5_hasher, depsname='.deps',
                 quiet=False):
        """ Initialise a Builder with the given options.

        "runner" specifies how programs should be run.  It is either a
            callable compatible with the Runner class, or a string selecting
            one of the standard runners ("atimes_runner", "strace_runner",
            "always_runner", or "smart_runner").
        "dirs" is a list of paths to look for dependencies (or outputs) in
            if using the strace or atimes runners.
        "dirdepth" is the depth to recurse into the paths in "dirs" (default
            essentially means infinitely). Set to 1 to just look at the
            immediate paths in "dirs" and not recurse at all. This can be
            useful to speed up the AtimesRunner if you're building in a large
            tree and you don't care about all of the subdirectories.
        "ignoreprefix" prevents recursion into directories that start with
            prefix.  It defaults to '.' to ignore svn directories.
            Change it to '_svn' if you use _svn hidden directories.
        "ignore" is a regular expression.  Any dependency that contains a
            regex match is ignored and not put into the dependency list.
            Note that the regex may be VERBOSE (spaces are ignored and # line
            comments allowed -- use \ prefix to insert these characters)
        "hasher" is a function which returns a string which changes when
            the contents of its filename argument changes, or None on error.
            Default is md5_hasher, but can also be mtime_hasher.
        "depsname" is the name of the JSON dependency file to load/save.
        "quiet" set to True tells the builder to not display the commands being
            executed (or other non-error output).
        """
        if runner is not None:
            self.set_runner(runner)
        elif hasattr(self, 'runner'):
            # For backwards compatibility, if a derived class has
            # defined a "runner" method then use it:
            pass
        else:
            self.runner = SmartRunner(self)
        if dirs is None:
            dirs = [os.path.abspath('.')]
        self.dirs = dirs
        self.dirdepth = dirdepth
        self.ignoreprefix = ignoreprefix
        if ignore is None:
            ignore = r'$x^'         # something that can't match
        self.ignore = re.compile(ignore, re.VERBOSE)
        self.depsname = depsname
        self.hasher = hasher
        self.quiet = quiet
        self.checking = False

    def echo(self, message):
        """ Print message, but only if builder is not in quiet mode. """
        if not self.quiet:
            print message

    def echo_command(self, command):
        """ Show a command being executed. """
        self.echo(command)

    def echo_delete(self, filename, error=None):
        """ Show a file being deleted. For subclassing Builder and overriding
            this function, the exception is passed in if an OSError occurs
            while deleting a file. """
        if error is None:
            self.echo('deleting %s' % filename)

    def run(self, *args, **kwargs):
        """ Run command given in args with kwargs per shell(), but only if its
            dependencies or outputs have changed or don't exist. """
        return self.run_multiple(args, **kwargs)

    def run_multiple(self, *arglists, **kwargs):
        """ Run each list given in args as per shell(), but only if
            its dependencies or outputs have changed or don't exist. """
        if not arglists:
            raise TypeError('run_multiple() takes at least 1 argument (0 given)')
        
        arglists = map(args_to_list, arglists)
        # we want a command line string for the .deps file key and for display
        commands = [subprocess.list2cmdline(arglist) for arglist in arglists]
        command_key = os.getcwd() + ':' + '; '.join(commands)

        if not self.key_outofdate(command_key):
            return

        # if just checking up-to-date-ness, set flag and do nothing more
        self.outofdate_flag = True
        if self.checking:
            return

        # use runner to run command and collect dependencies
        deps = set()
        outputs = set()
        for command, arglist in zip(commands, arglists):
            self.echo_command(command)
            cdeps, coutputs = self.runner(*arglist, **kwargs)
            deps.update(cdeps)
            outputs.update(coutputs)
        
        if deps or outputs:
            deps_dict = {}
            # hash the dependency inputs and outputs
            for dep in deps:
                hashed = self.hasher(dep)
                if hashed is not None:
                    deps_dict[dep] = "input-" + hashed
            for output in outputs:
                hashed = self.hasher(output)
                if hashed is not None:
                    deps_dict[output] = "output-" + hashed
            self.deps[command_key] = deps_dict

    def memoize(self, command, **kwargs):
        """ Run the given command, but only if its dependencies have changed --
            like run(), but returns the status code instead of raising an
            exception on error. If "command" is a string (as per memoize.py)
            it's split into args using shlex.split() in a POSIX/bash style,
            otherwise it's a list of args as per run().

            This function is for compatiblity with memoize.py and is
            deprecated. Use run() instead. """
        if isinstance(command, basestring):
            args = shlex.split(command)
        else:
            args = args_to_list(command)
        try:
            self.run(args, **kwargs)
            return 0
        except ExecutionError, exc:
            message, data, status = exc
            return status

    def outofdate(self, func):
        """ Return True if given build function is out of date. """
        self.checking = True
        self.outofdate_flag = False
        func()
        self.checking = False
        return self.outofdate_flag

    def key_outofdate(self, key):
        """ Return True if given key (command line + cwd) is out of date. """
        if key in self.deps:
            # command has been run before, see if deps have changed
            for dep, oldhash in self.deps[key].items():
                assert oldhash.startswith('input-') or \
                       oldhash.startswith('output-'), \
                    "%s file corrupt, do a clean!" % self.depsname
                oldhash = oldhash.split('-', 1)[1]
                # make sure this dependency or output hasn't changed
                newhash = self.hasher(dep)
                if newhash is None or newhash != oldhash:
                    break
            else:
                # all dependencies are unchanged
                return False
        # command has never been run, or one of the dependencies didn't
        # exist or had changed
        return True

    def autoclean(self):
        """ Automatically delete all outputs of this build as well as the .deps
            file. """
        # first build a list of all the outputs from the .deps file
        outputs = []
        for command, deps in self.deps.items():
            outputs.extend(dep for dep, hashed in deps.items()
                           if hashed.startswith('output-'))
        outputs.append(self.depsname)
        self._deps = None
        for output in outputs:
            try:
                os.remove(output)
            except OSError, e:
                self.echo_delete(output, e)
            else:
                self.echo_delete(output)

    @property
    def deps(self):
        """ Lazy load .deps file so that instantiating a Builder is "safe". """
        if not hasattr(self, '_deps') or self._deps is None:
            self.read_deps()
            atexit.register(self.write_deps, depsname=os.path.abspath(self.depsname))
        return self._deps

    def read_deps(self):
        """ Read dependency JSON file into deps object. """
        try:
            f = open(self.depsname)
            try:
                self._deps = json.load(f)
                # make sure the version is correct
                if self._deps.get('.deps_version', 0) != deps_version:
                    printerr('Bad %s dependency file version! Rebuilding.'
                             % self.depsname)
                    self._deps = {}
                self._deps.pop('.deps_version', None)
            finally:
                f.close()
        except IOError:
            self._deps = {}

    def write_deps(self, depsname=None):
        """ Write out deps object into JSON dependency file. """
        if self._deps is None:
            return                      # we've cleaned so nothing to save
        self.deps['.deps_version'] = deps_version
        if depsname is None:
            depsname = self.depsname
        f = open(depsname, 'w')
        try:
            json.dump(self.deps, f, indent=4, sort_keys=True)
        finally:
            f.close()
            self._deps.pop('.deps_version', None)

    _runner_map = {
        'atimes_runner' : AtimesRunner,
        'strace_runner' : StraceRunner,
        'always_runner' : AlwaysRunner,
        'smart_runner' : SmartRunner,
        }

    def set_runner(self, runner):
        """Set the runner for this builder.  "runner" is either a Runner
           subclass (e.g. SmartRunner), or a string selecting one of the
           standard runners ("atimes_runner", "strace_runner",
           "always_runner", or "smart_runner")."""
        try:
            self.runner = self._runner_map[runner](self)
        except KeyError:
            if isinstance(runner, basestring):
                # For backwards compatibility, allow runner to be the
                # name of a method in a derived class:
                self.runner = getattr(self, runner)
            else:
                # pass builder to runner class to get a runner instance
                self.runner = runner(self)

    def _is_relevant(self, fullname):
        """ Return True if file is in the dependency search directories. """

        # need to abspath to compare rel paths with abs
        fullname = os.path.abspath(fullname)
        for path in self.dirs:
            path = os.path.abspath(path)
            if fullname.startswith(path):
                rest = fullname[len(path):]
                # files in dirs starting with ignoreprefix are not relevant
                if os.sep+self.ignoreprefix in os.sep+os.path.dirname(rest):
                    continue
                # files deeper than dirdepth are not relevant
                if rest.count(os.sep) > self.dirdepth:
                    continue
                return True
        return False

# default Builder instance, used by helper run() and main() helper functions
default_builder = Builder()
default_command = 'build'

def setup(builder=None, default=None, **kwargs):
    """ Setup the default Builder (or an instance of given builder if "builder"
        is not None) with the same keyword arguments as for Builder().
        "default" is the name of the default function to run when the build
        script is run with no command line arguments. """
    global default_builder, default_command
    if builder is not None:
        default_builder = builder()
    if default is not None:
        default_command = default
    default_builder.__init__(**kwargs)
setup.__doc__ += '\n\n' + Builder.__init__.__doc__

def run(*args, **kwargs):
    """ Run the given command, but only if its dependencies have changed. Uses
        the default Builder. """
    default_builder.run(*args, **kwargs)

def run_multiple(*args, **kwargs):
    """ Run the given commands, but only if their dependencies have changed.
        Uses the default Builder. """
    default_builder.run_multiple(*args, **kwargs)

def autoclean():
    """ Automatically delete all outputs of the default build. """
    default_builder.autoclean()

def memoize(command, **kwargs):
    return default_builder.memoize(command, **kwargs)

memoize.__doc__ = Builder.memoize.__doc__

def outofdate(command):
    """ Return True if given command is out of date and needs to be run. """
    return default_builder.outofdate(command)

def parse_options(usage, extra_options=None):
    """ Parse command line options and return (parser, options, args). """
    parser = optparse.OptionParser(usage='Usage: %prog '+usage,
                                   version='%prog '+__version__)
    parser.disable_interspersed_args()
    parser.add_option('-t', '--time', action='store_true',
                      help='use file modification times instead of MD5 sums')
    parser.add_option('-d', '--dir', action='append',
                      help='add DIR to list of relevant directories')
    parser.add_option('-c', '--clean', action='store_true',
                      help='autoclean build outputs before running')
    parser.add_option('-q', '--quiet', action='store_true',
                      help="don't echo commands, only print errors")
    parser.add_option('-k', '--keep', action='store_true',
                      help='keep temporary strace output files')
    if extra_options:
        # add any user-specified options passed in via main()
        for option in extra_options:
            parser.add_option(option)
    options, args = parser.parse_args()
    default_builder.quiet = options.quiet
    if options.time:
        default_builder.hasher = mtime_hasher
    if options.dir:
        default_builder.dirs += options.dir
    if options.clean:
        default_builder.autoclean()
    if options.keep:
        StraceRunner.keep_temps = InterposingRunner.keep_temps = options.keep
    return parser, options, args

def fabricate_version(min=None, max=None):
    """ If min is given, assert that the running fabricate is at least that
        version or exit with an error message. If max is given, assert that
        the running fabricate is at most that version. Return the current
        fabricate version string. This function was introduced in v1.14;
        for prior versions, the version string is available only as module
        local string fabricate.__version__ """

    if min is not None and float(__version__) < min:
        sys.stderr.write(("fabricate is version %s.  This build script "
            "requires at least version %.2f") % (__version__, min))
        sys.exit()
    if max is not None and float(__version__) > max:
        sys.stderr.write(("fabricate is version %s.  This build script "
            "requires at most version %.2f") % (__version__, max))
        sys.exit()
    return __version__

def main(globals_dict=None, build_dir=None, extra_options=None):
    """ Run the default function or the function(s) named in the command line
        arguments. Call this at the end of your build script. If one of the
        functions returns nonzero, main will exit with the last nonzero return
        value as its status code.

        extra_options is an optional list of options created with
        optparse.make_option(). The pseudo-global variable main.options
        is set to the parsed options list.
    """
    usage = '[options] build script functions to run'
    parser, options, actions = parse_options(usage, extra_options)
    main.options = options
    if not actions:
        actions = [default_command]

    original_path = os.getcwd()
    if None in [globals_dict, build_dir]:
        try:
            frame = sys._getframe(1)
        except:
            printerr("Your Python version doesn't support sys._getframe(1),")
            printerr("call main(globals(), build_dir) explicitly")
            sys.exit(1)
        if globals_dict is None:
            globals_dict = frame.f_globals
        if build_dir is None:
            build_file = frame.f_globals.get('__file__', None)
            if build_file:
                build_dir = os.path.dirname(build_file)
    if build_dir:
        if not options.quiet and os.path.abspath(build_dir) != original_path:
            print "Entering directory '%s'" % build_dir
        os.chdir(build_dir)

    status = 0
    try:
        for action in actions:
            if '(' not in action:
                action = action.strip() + '()'
            name = action.split('(')[0].split('.')[0]
            if name in globals_dict:
                this_status = eval(action, globals_dict)
                if this_status:
                    status = int(this_status)
            else:
                printerr('%r command not defined!' % action)
                sys.exit(1)
    except ExecutionError, exc:
        message, data, status = exc
        printerr('fabricate: ' + message)
    finally:
        if not options.quiet and os.path.abspath(build_dir) != original_path:
            print "Leaving directory '%s' back to '%s'" % (build_dir, original_path)
        os.chdir(original_path)
    sys.exit(status)

if __name__ == '__main__':
    # if called as a script, emulate memoize.py -- run() command line
    parser, options, args = parse_options('[options] command line to run')
    status = 0
    if args:
        status = memoize(args)
    elif not options.clean:
        parser.print_help()
        status = 1
    # autoclean may have been used
    sys.exit(status)
