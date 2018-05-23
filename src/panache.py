#!/usr/bin/env python3
# -*- coding: utf-8 -*-

""" panache: Pandoc wrapped in styles
for more info: <https://github.com/sebogh/panache>
Author    : Sebastian Bogan <sebogh@qibli.net>
Copyright : Copyright 2017, Sebastian Bogan
License   : BSD3
"""

import os
import errno
import sys
import tempfile
import re
import glob
import yaml
import logging
import pystache
import xml.etree.ElementTree
from subprocess import PIPE, run
from optparse import OptionParser, BadOptionError, AmbiguousOptionError
from datetime import datetime
from yaml.scanner import ScannerError

# check script environment
if getattr(sys, 'frozen', False):
    script = os.path.realpath(sys.executable).replace(os.path.sep, '/')
elif __file__:
    script = os.path.realpath(__file__).replace(os.path.sep, '/')
script_dir = os.path.dirname(script)
script_base = os.path.basename(script)
user_home = os.path.expanduser("~")
default_style_dir = os.path.join(user_home, ".panache").replace(os.path.sep, '/')
version = "0.2.4"

# setup logging
logging.basicConfig(format="%(message)s")

# defined subprocess environment
subprocess_environment = os.environ.copy()
subprocess_environment['LANG'] = 'en_US.UTF-8'


def vcs_lookup(input_path):

    if not input_path:
        return ''

    abs_path = os.path.abspath(input_path)
    abs_dir = os.path.dirname(abs_path)

    # try Git
    try:

        # try Git
        cmd = ['git', '-C', abs_dir, 'log', '-1', '--date=iso', '--format="%h;%cd"', abs_path]
        p = run(cmd, stdout=PIPE, stderr=PIPE, env=subprocess_environment)

        if p.returncode == 0:

            # if Git-call succeeded
            logging.debug("Input is part of a Git repo.")
            stdout = p.stdout.decode("utf8")
            match = re.match(r'"(.+);(.+)"\n', stdout)

            if match:
                revision = match.group(1)
                dt = datetime.strptime(match.group(2), '%Y-%m-%d %H:%M:%S %z')
                dt = dt - dt.utcoffset()
                formatted_date = dt.strftime('%Y-%m-%dT%H:%M:%SZ')
                return revision, formatted_date
        else:

            # if Git-call failed
            stderr = p.stderr.decode("utf8")
            if stderr.find('Not a git repository') > 0:

                # if Git failed, because it is not a GIT repo
                logging.debug("Input is not part of a Git repo.")

                # try SVN
                cmd = ['svn', '--non-interactive', 'info', '--xml', abs_path]
                p = run(cmd, stdout=PIPE, stderr=PIPE, env=subprocess_environment)

                if p.returncode == 0:
                    logging.debug("Input is part of a SVN repo.")
                    stdout = p.stdout.decode("utf8")
                    root = xml.etree.ElementTree.fromstring(stdout)
                    revision = formatted_date = None
                    entry_attr = root.find('entry').attrib
                    if entry_attr:
                        revision = entry_attr['revision']
                    entry = root.find('.//date')
                    if entry is not None:
                        date = datetime.strptime(entry.text, '%Y-%m-%dT%H:%M:%S.%fZ')
                        formatted_date = date.strftime('%Y-%m-%dT%H:%M:%SZ')
                    if revision and formatted_date:
                        return revision, formatted_date

                else:

                    # if SVN-call failed
                    stderr = p.stderr.decode("utf8")
                    if stderr.find('is not a working copy') > 0:

                        # if SVN failed, because it is not a SVN repo
                        logging.debug("Input is not part of a SVN repo.")

                    else:

                        # if it may be a SVN repo but the SVN call failed for some other reason
                        logging.debug("Error calling SVN. %s" % stderr)

            else:

                # if it may be a Git repo but the Git call failed for some other reason
                logging.debug("Error calling Git. %s" % stderr)

    except Exception as e:
        logging.debug("Error getting VCS info. %s" % e)

    return '', ''

# panache-specific YAML words
STYLEDEF_ = 'styledef_'
STYLES_ = 'styles_'
PARENT_ = 'parent'
COMMANDLINE_ = 'commandline'
METADATA_ = 'metadata'
FILTER_ = 'filter'
RUN_ = 'run'
KILL_ = 'kill'

panache_yaml_format_variables = {
    'STYLEDEF_': STYLEDEF_,
    'STYLES_': STYLES_,
    'PARENT_': PARENT_,
    'COMMANDLINE_': COMMANDLINE_,
    'METADATA_': METADATA_,
    'FILTER_': FILTER_,
    'RUN_': RUN_,
    'KILL_': KILL_
}


# https://stackoverflow.com/a/26853961
def merge_two_dicts(x, y):
    z = x.copy()       
    z.update(y)    
    return z
        

# https://stackoverflow.com/a/9307174
class PassThroughOptionParser(OptionParser):
    """
    An unknown option pass-through implementation of OptionParser.

    When unknown arguments are encountered, bundle with largs and try again,
    until rargs is depleted.

    sys.exit(status) will still be called if a known argument is passed
    incorrectly (e.g. missing arguments or bad argument types, etc.)
    """
    def _process_args(self, largs, rargs, values):
        while rargs:
            try:
                OptionParser._process_args(self, largs, rargs, values)
            except (BadOptionError, AmbiguousOptionError) as e:
                largs.append(e.opt_str)


class PanacheException(Exception):

    def __init__(self, message, code=0):
        self.code = code
        self.message = message


class PanacheStyle:

    def __init__(self, name, data=None, source=None):

        # style name
        assert name
        self.name = name

        self.parent = None
        self.commandline = dict()
        self.metadata = dict()
        self.filters_run = list()
        self.filters_kill = list()

        # parent
        if data and PARENT_ in data:
            self.parent = data[PARENT_]

        # commandline
        if (data
                and COMMANDLINE_ in data
                and isinstance(data[COMMANDLINE_], dict)):
            self.commandline = data[COMMANDLINE_]

        # metadata
        if (data
                and METADATA_ in data
                and isinstance(data[METADATA_], dict)):
            self.metadata = data[METADATA_]

        # filter
        if (data
                and FILTER_ in data
                and isinstance(data[FILTER_], dict)):
            if (RUN_ in data[FILTER_]
                    and isinstance(data[FILTER_][RUN_], list)):
                self.filters_run = data[FILTER_][RUN_]
            if (KILL_ in data[FILTER_]
                    and isinstance(data[FILTER_][KILL_], list)):
                self.filters_kill = data[FILTER_][KILL_]

        self.source = source


class PanacheStyles:

    def __init__(self, style_vars):
        self.styles = dict()
        self.style_vars = style_vars

    def load(self, style_dir):

        yaml_paths = glob.glob(os.path.join(style_dir, '*.yaml'))

        # for each '*.yaml'-file in the data directory
        for path in sorted(yaml_paths):

            with open(path, 'r', encoding='utf-8') as f:

                # read the file
                content = f.read()

                # render content (apply substitutions)
                rendered_content = pystache.render(content, self.style_vars)

                # load YAML-data
                try:
                    data = yaml.load(rendered_content)
                except ScannerError as e:
                    raise PanacheException("YAML error in %s:\n%s\n" % (f.name, e), 200)

                # if YAML contains style definitions
                if STYLEDEF_ in data:

                    stylefile_basename = os.path.basename(path)

                    # add each new one
                    for style_name in data[STYLEDEF_]:

                        if style_name not in self.styles:

                            logging.debug("  Adding '%s' (found in '%s')." % (style_name, stylefile_basename))

                            self.styles[style_name] = \
                                PanacheStyle(style_name, data[STYLEDEF_][style_name], path)

                        else:

                            logging.warning("Ignoring duplicate definition of '%s' (found in'%s')."
                                            % (style_name, stylefile_basename))

    def update(self, update):

        style_name = update.name
        path = update.source
        stylefile_basename = os.path.basename(path)

        if style_name not in self.styles:

            logging.debug("  Adding '%s' (found in '%s')." % (style_name, stylefile_basename))

            self.styles[style_name] = update

        else:
            style = self.styles[style_name]

            logging.debug("  Merging '%s' (found in '%s')." % (style_name, stylefile_basename))

            style.commandline = merge_two_dicts(style.commandline, update.commandline)
            style.metadata = merge_two_dicts(style.metadata, update.metadata)
            style.filters_run = style.filters_run + update.filters_run
            style.filters_kill = style.filters_kill + update.filters_kill

    def resolve(self, style_name):

        if not style_name:
            return {COMMANDLINE_: dict(), METADATA_: dict(), FILTER_: list()}

        if style_name not in self.styles:
            logging.warning("  Unknown style '%s'" % style_name)
            return {COMMANDLINE_: dict(), METADATA_: dict(), FILTER_: list()}

        style = self.styles[style_name]

        # compute the parent
        parent = self.resolve(style.parent)

        # merge styles
        commandline = merge_two_dicts(parent[COMMANDLINE_], style.commandline)
        metadata = merge_two_dicts(parent[METADATA_], style.metadata)
        filters = list(filter(lambda x: x not in style.filters_kill, parent[FILTER_] + style.filters_run))

        return {COMMANDLINE_: commandline, METADATA_: metadata, FILTER_: filters}


def parse_cmdline(cl):
    """Parse and validate the command line.
    """

    usage = "%s [<OPTIONS>] [<PANDOC-OPTIONS>]" % script_base
    parser = PassThroughOptionParser(usage, add_help_option=False)
    parser.add_option("--input", dest="input", default="")
    parser.add_option("--output", dest="output", default="")
    parser.add_option("-h", "--help", dest="help", action="store_true", default=False)
    parser.add_option("--style", dest="style", default="")
    parser.add_option("--medium", dest="medium", default="")
    parser.add_option("--debug", dest="debug", action="store_true", default=False)
    parser.add_option("--verbose", dest="verbose", action="store_true", default=False)
    parser.add_option("--version", dest="version", action="store_true", default=False)
    parser.add_option("--disable-vcs-lookup", dest="disable_vcs_lookup", action="store_true", default=False)
    parser.add_option("--style-dir", dest="style_dir")
    parser.add_option("--style-var", dest="style_vars", action="append", default=[])

    (options, args) = parser.parse_args(cl)

    if options.version:
        os.sys.stderr.write("""panache {version}
Default style directory: '{default_style_dir}'
Copyright (C) 2006-2018 Sebastian Bogan
Web: https://github.com/sebogh/panache
""".format(**{'version': version, 'default_style_dir': default_style_dir}))
        sys.exit(0)

    if options.help:
        os.sys.stderr.write("""
NAME

    {name}

SYNOPSIS

    {usage}

DESCRIPTION

    Pandoc wrapper implementing styles.

OPTIONS

    --input=<PATH>
        The input path. Default STDIN.
    --output=<PATH>
        The output path. Default STDOUT.
    --medium=<MEDIUM>
        The target medium.
    --style=<STYLE>
        The fallback style to use, if --medium is not specified or
        the input doesn't specify a style for the given medium. 
    --style-dir=<PATH>
        Where to find style definitions.
        (Default: '{default_style_dir}'). 
    --style-var=<KEY>:<VALUE>    
        A variable that should be replaced in the style template.
        May be used several times. If the same key is used several 
        times, then the variable is interpreted as list of values.
    --disable-vcs-lookup
        Don't try to get VCS reference and last change date.
    --verbose
        Print verbose info (to STDERR).
    --debug
        Print all debug info (to STDERR).
    --version
        Print panache version info and exit.
    -h, --help
        Print this help message.

PANDOC-OPTIONS

    Any argument not being one of the above options is passed down to Pandoc. 

AUTHOR

    Sebastian Bogan sebogh@qibli.net

""".format(**{'name': script_base, 'usage': usage, 'default_style_dir': default_style_dir}))
        sys.exit(0)

    if options.verbose:
        logging.getLogger().setLevel(logging.INFO)

    if options.debug:
        logging.getLogger().setLevel(logging.DEBUG)



    # path to the input- and output-file
    if options.input:
        options.input = os.path.abspath(options.input).replace(os.path.sep, '/')
        if not os.path.isfile(options.input):
            raise PanacheException("No such file '%s'." % options.input, 102)

    if options.output:
        options.output = os.path.abspath(options.output).replace(os.path.sep, '/')

    # check style-dir
    if options.style_dir:
        options.style_dir = os.path.abspath(options.style_dir).replace(os.path.sep, '/')
        if not os.path.isdir(options.style_dir):
            raise PanacheException("No such directory '%s'." % options.style_dir, 103)
    elif os.path.isdir(default_style_dir):
        options.style_dir = default_style_dir

    # default style variables
    style_vars = {
        'panache_dir': script_dir,
        'panache_version_%s' % version: True,
        'os_%s' % os.name: True,
        'build_date': '%s' % datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'input_dir': '',
        'input_basename': '',
        'input_basename_root': '',
        'input_basename_extension': '',
    }

    # if we have a style dir
    if options.style_dir:
        style_vars['style_dir'] = options.style_dir

    # extend default style variables if we have an input
    if options.input:
        input_dir = os.path.dirname(options.input)
        input_basename = os.path.basename(options.input)
        input_basename_root, input_basename_extension = os.path.splitext(input_basename)
        style_vars['input_dir'] = input_dir
        style_vars['input_basename'] = input_basename
        style_vars['input_basename_root'] = input_basename_root
        style_vars['input_basename_extension'] = input_basename_extension

    # extend default style variables if we have an output
    if options.output:
        output_dir = os.path.dirname(options.output)
        output_basename = os.path.basename(options.output)
        output_basename_root, output_basename_extension = os.path.splitext(output_basename)
        style_vars['output_dir'] = output_dir
        style_vars['output_basename'] = output_basename
        style_vars['output_basename_root'] = output_basename_root
        style_vars['output_basename_extension'] = output_basename_extension

    # add style variables from the command line
    # style variables must follow the rules of template strings
    # see: https://docs.python.org/3.5/library/string.html#template-strings
    style_var_pattern = re.compile('^([a-z_]+):(.*)$', flags=0)
    for style_var in options.style_vars:
        match = style_var_pattern.match(style_var)
        if not match:
            raise PanacheException("Invalid style variable '%s'." % style_var, 104)
        key = match.group(1)
        value = match.group(2)
        if key not in style_vars:
            style_vars[key] = value
        elif isinstance(style_vars[key], list):
            list(style_vars[key]).append(value)
        else:
            style_vars[key] = [style_vars[key], value]

    return options, args, style_vars


# see: https://stackoverflow.com/a/10840586
def silent_remove(filename):
    """" Remove a file if it exists.
    """
    try:
        os.remove(filename)
    except OSError as e:

        # filter out errno.ENOENT (no such file or directory)
        if e.errno != errno.ENOENT:

            # but re-raise any other
            raise


def get_yaml_lines(lines):
    """" Strip `lines' to those lines that are YAML.
    """
    start = re.compile('^[-]{3}\s*$', flags=0)
    stop = re.compile('^[-.]{3}\s*$', flags=0)
    in_yaml = False
    yaml_lines = list()
    for line in lines:
        if not in_yaml:
            if start.match(line):
                in_yaml = True
        else:
            if stop.match(line):
                in_yaml = False
            else:
                yaml_lines.append(line)
    return yaml_lines


def get_input_yaml(file, style_vars):
    """" Get YAML from a Pandoc-flavored Markdown file.
    """

    # read lines from file
    with open(file, "r", encoding='utf-8') as f:
        lines = f.readlines()

    # strip lines to those that are YAML
    yaml_lines = get_yaml_lines(lines)
    if not yaml_lines:
        return None

    # read the file
    content = ''.join(yaml_lines)

    # render content (apply substitutions)
    rendered_content = pystache.render(content, style_vars)

    # load YAML-data

    try:
        data = yaml.load(rendered_content)
    except ScannerError as e:
        raise PanacheException("YAML error in input:\n%s\n" % e, 200)

    return data


def determine_style(options, input_yaml):
    """ Determine the style to use.
    """

    # try the challenge response
    if (options
            and input_yaml
            and options.medium
            and STYLES_ in input_yaml
            and options.medium in input_yaml[STYLES_]):
        return input_yaml[STYLES_][options.medium]

    # if challange response fails try fallback
    if options and options.style:
        return options.style

    return None


def compile_command_line(input_file, metadata_file, parameters, options, args):

    # compile command line
    command = ["pandoc", input_file]

    if metadata_file:
        command.append(metadata_file)
    if options.output:
        command.append('--output=%s' % options.output)
    for key, value in parameters[COMMANDLINE_].items():
        if isinstance(parameters[COMMANDLINE_][key], bool):
            if parameters[COMMANDLINE_][key]:
                command.append('--%s' % key)
        else:
            command.append('--%s=%s' % (key, value))
    for run_filter in parameters[FILTER_]:
        command.append('--filter=%s' % run_filter)

    command.extend(list(args))

    return command


def main():

    try:

        # ensure correct python version
        if sys.version_info.major < 3 or sys.version_info.minor < 5:
            raise PanacheException("Wrong Python version (%d.%d). Need Python >= 3.5"
                                   % (sys.version_info.major, sys.version_info.minor), 300)

        # parse and validate command line
        options, args, style_vars = parse_cmdline(sys.argv[1:])
        logging.info("Panache %s ('%s')" % (version, script))
        logging.debug("Parsed commandline.")

        # get vcs-info
        if options.input and not options.disable_vcs_lookup:
            vcs_reference, vcs_date = vcs_lookup(options.input)
            if vcs_reference:
                style_vars['vcsreference'] = vcs_reference
            if vcs_date:
                style_vars['vcsdate'] = vcs_date

        # initialize styles from the data directory
        panache_styles = PanacheStyles(style_vars)
        if options.style_dir:
            logging.debug("Loading styles:")
            panache_styles.load(options.style_dir)

        # copy STDIN to a temporary file, iff needed
        input_file = options.input
        if not options.input:
            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(sys.stdin.buffer.read())
                input_file = f.name.replace(os.path.sep, '/')
                logging.debug("Copied STDIN to temp. file '%s'." % input_file)

        # load YAML from input (either the temporary file or the one name on the command line)
        input_yaml = get_input_yaml(input_file, style_vars)

        # update (or add) style definitions based on definitions in the input file
        if input_yaml and STYLEDEF_ in input_yaml:
            if input_yaml[STYLEDEF_]:
                logging.debug("Updating styles:")
                for style_name in input_yaml[STYLEDEF_]:
                    panache_styles.update(PanacheStyle(style_name, input_yaml[STYLEDEF_][style_name], input_file))

        # determine desired style
        style = determine_style(options, input_yaml)
        if style:
            logging.info("Computed style '%s'." % style)
        else:
            logging.info("Couldn't compute a style.")

        # resolve style to Pandoc compile parameters (and metadata)
        parameters = panache_styles.resolve(style)
        logging.debug("Resolving style '%s'." % style)

        # all stylevariables become metadata (wich may be overwritten by the style)
        parameters[METADATA_] = merge_two_dicts(style_vars, parameters[METADATA_])

        # write the computed metadata to a temporary file
        with tempfile.NamedTemporaryFile(delete=False) as f:

            # make sure YAML starts in a new line
            f.write(b'\n\n')
            metadata = yaml.dump(parameters[METADATA_],
                                 default_flow_style=False,
                                 encoding='utf-8',
                                 explicit_start=True,
                                 explicit_end=True)
            f.write(metadata)
            metadata_file = f.name.replace(os.path.sep, '/')
            logging.info("Wrote following metadata to temp. file '%s'.\n  %s"
                         % (metadata_file, metadata.decode().rstrip().replace("\n", "\n  ")))

        # compile the command
        command = compile_command_line(input_file, metadata_file, parameters, options, args)

        # change to the directory containing the input, if not STDIN
        if options.input:
            working_directory = os.path.dirname(options.input)
            logging.debug("Changing directory to '%s'." % working_directory)
            os.chdir(working_directory)

        # run the command
        logging.info("Running:\n  %s" % ' '.join(command))
        p = run(command, stdout=sys.stdout, stderr=sys.stderr, env=subprocess_environment)

        if p.returncode == 0:
            if options.output:
                logging.info("Created:\n  %s." % options.output)
        else:
            sys.exit(1)

        # delete the temporary files
        silent_remove(metadata_file)
        if not options.input:
            silent_remove(input_file)

    except PanacheException as e:
        sys.stderr.write(e.message)
        sys.exit(e.code)
    except Exception as e:
        sys.stderr.write(e)
        sys.exit(1)


if __name__ == "__main__":
    main()

    
