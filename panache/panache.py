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
from datetime import datetime
from subprocess import Popen
from typing import List, Dict
from string import Template

from optparse import OptionParser, BadOptionError, AmbiguousOptionError

# check script environment
script = os.path.realpath(sys.argv[0])
script_dir = os.path.dirname(script)
base_dir = os.path.realpath(os.path.join(script_dir, ".."))
script_base = os.path.basename(script)

# panache-specific YAML words
STYLEDEF_ = 'styledef_'
STYLES_ = 'styles_'
STYLE_ = 'style_'
PARENT_ = 'parent'
COMMANDLINE_ = 'commandline'
METADATA_ = 'metadata'
FILTER_ = 'filter'
RUN_ = 'run'
KILL_ = 'kill'

panache_yaml_format_variables = {
    'STYLEDEF_': STYLEDEF_,
    'STYLES_': STYLES_,
    'STYLE_': STYLE_,
    'PARENT_': PARENT_,
    'COMMANDLINE_': COMMANDLINE_,
    'METADATA_': METADATA_,
    'FILTER_': FILTER_,
    'RUN_': RUN_,
    'KILL_': KILL_
}


class PassThroughOptionParser(OptionParser):
    """
    see: https://stackoverflow.com/a/9307174

    An unknown option pass-through implementation of OptionParser.

    When unknown arguments are encountered, bundle with largs and try again,
    until rargs is depleted.

    sys.exit(status) will still be called if a known argument is passed
    incorrectly (e.g. missing arguments or bad argument types, etc.)
    """
    def _process_args(self, largs, rargs, values):
        while rargs:
            try:
                OptionParser._process_args(self,largs,rargs,values)
            except (BadOptionError,AmbiguousOptionError) as e:
                largs.append(e.opt_str)

class PanacheException(Exception):

    def __init__(self, message: str, code: int = 0):
        self.code = code
        self.message = message

class PanacheStyle:

    def __init__(self, name: str, data: Dict = None, source: str = None):

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

    def __init__(self):
        self.styles = dict()

    def load(self, style_dir):

        # for each '*.yaml'-file in the data directory
        for path in glob.glob(os.path.join(style_dir, '*.yaml')):

            with open(path, 'r', encoding='utf-8') as f:

                # try to load YAML-data from file
                try:

                    # load YAML-data
                    data = yaml.load(f)

                    # if YAML contains style definitions
                    if STYLEDEF_ in data:

                        # add each new one
                        for style_name in data[STYLEDEF_]:

                            if style_name not in self.styles:

                                logging.info("Adding definition of style '%s' (found in '%s')."
                                             % (style_name, path))

                                self.styles[style_name] = \
                                    PanacheStyle(style_name, data[STYLEDEF_][style_name], path)

                            else:

                                logging.warning("Ignoring duplicate definition of '%s' (found in'%s')."
                                                % (style_name, path))

                except Exception as e:
                    logging.warning(e)
                    pass

    def update(self, update):

        style_name = update.name
        path = update.source

        if style_name not in self.styles:

            logging.info("Adding definition of style '%s' (found in '%s')." % (style_name, path))

            self.styles[style_name] = update

        else:
            style = self.styles[style_name]

            logging.info("Merging definition of style '%s' (found in '%s')." % (style_name, path))

            style.parent = update.parent
            style.commandline = {**style.commandline, **update.commandline}
            style.metadata = {**style.metadata, **update.metadata}
            style.filters_run = style.filters_run + update.filters_run
            style.filters_kill = style.filters_run + update.filters_kill

    def resolve(self, style_name):

        if not style_name:
            return {COMMANDLINE_: dict(), METADATA_: dict(), FILTER_: list()}

        if style_name not in self.styles:
            logging.warning("Unknown style '%s'" % style_name)
            return {COMMANDLINE_: dict(), METADATA_: dict(), FILTER_: list()}

        style = self.styles[style_name]

        # compute the parent
        parent = self.resolve(style.parent)

        # merge styles
        commandline = {**parent[COMMANDLINE_], **style.commandline}
        metadata = {**parent[METADATA_], **style.metadata}
        filters = list(filter(lambda x: x in style.filters_kill, parent[FILTER_] + style.filters_run))

        return {COMMANDLINE_: commandline, METADATA_: metadata, FILTER_: filters}

def parse_cmdline(cl: List[str]):
    """Parse and validate the command line.
    """

    default_style_dir = base_dir
    if not os.path.isdir(default_style_dir):
        default_style_dir = None

    usage = "%s [<OPTIONS>] [<PANDOC-OPTIONS>]" % script_base
    parser = PassThroughOptionParser(usage, add_help_option=False)
    parser.add_option("--input", dest="input", default="")
    parser.add_option("--output", dest="output", default="")
    parser.add_option("-h", "--help", dest="help", action="store_true", default=False)
    parser.add_option("--style", dest="style", default="")
    parser.add_option("--medium", dest="medium", default="")
    parser.add_option("--debug", dest="debug", action="store_true", default=False)
    parser.add_option("--style-dir", dest="style_dir", default=default_style_dir)
    parser.add_option("--style-var", dest="style_vars", action="append", default=[])

    (options, args) = parser.parse_args(cl)

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
    --style=<STYLE>
        The style to use.
    --medium=<MEDIUM>
        The target medium.
    --style-dir=<PATH>
        Where to find style definitions. 
        (Default: '{default_style_dir}'). 
    --style-var=<KEY>:<VALUE>    
        A variable that should be replaced in the style template.
        May be used several times.
    --debug
        Print the Pandoc command line to STDERR.
    -h, --help
        Print this help message.

PANDOC-OPTIONS

    Any argument not being one of the above options is passed down to Pandoc. 

AUTHOR

    Sebastian Bogan sebastian.bogan@t-systems.com

""".format(**{'name': script_base, 'usage': usage, 'default_style_dir': default_style_dir}))
        sys.exit(0)

    # path to the input- and output-file
    if options.input:
        options.input = os.path.abspath(options.input)
        if not os.path.isfile(options.input):
            raise PanacheException("No such file '%s'." % options.input, 102)

    if options.output:
        options.output = os.path.abspath(options.output)

    # check style-dir
    if options.style_dir:
        options.style_dir = os.path.abspath(options.style_dir)
        if not os.path.isdir(options.style_dir):
            raise PanacheException("No such directory '%s'." % options.style_dir, 103)

    if options.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # style variables must follow the rules of template strings
    # see: https://docs.python.org/3.5/library/string.html#template-strings
    style_var_pattern = re.compile('^([a-z_]+):(.*)$', flags=0)
    style_vars_dict = dict()
    for style_var in options.style_vars:
        match = style_var_pattern.match(style_var)
        if not match:
            raise PanacheException("Invalid style variable '%s'." % style_var, 104)
        style_vars_dict[match.group(1)] = match.group(2)


    return options, args, style_vars_dict


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


def get_yaml_lines(lines: list):
    """" Strip `lines' to those lines that are YAML.
    """
    start = re.compile('^[-]{3}\s*$', flags=0)
    stop = re.compile('^[-\.]{3}\s*$', flags=0)
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


def get_input_yaml(file):
    """" Get YAML from a Pandoc-flavored Markdown file.
    """

    # read lines from file
    with open(file, "r", encoding='utf-8') as f:
        lines = f.readlines()

    # strip lines to those that are YAML
    yaml_lines = get_yaml_lines(lines)
    if not yaml_lines:
        return None

    # load and return YAML data
    return yaml.load(''.join(yaml_lines))


def determine_style(options, input_yaml):
    """ Determine the style to use.
    """

    # a style named on the command line has highest priority
    if options.style:
        return options.style
    # if there is no style named on the command line a style named in the input would be used
    if STYLE_ in input_yaml:
        return input_yaml[STYLE_]
    # if there is no style named on the command line nor in the input a "medium" -> "style" match would be used
    if options.medium and STYLES_ in input_yaml and options.medium in input_yaml[STYLES_]:
        return input_yaml[STYLES_][options.medium]
    return None


def compile_command_line(input_file, metadata_file, parameters, options, args):

    # compile command line
    command = ["pandoc"]

    if metadata_file:
        command.append(metadata_file)
    command.append(input_file)
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

    # possibly print the command line
    if options.debug:
        sys.stderr.write("Running:\n  %s\n" % " ".join(command))
        sys.stderr.flush()

    return command


def add_special_meta(input_file, options, metadata):
    input_file_dir = os.path.dirname(input_file)
    input_file_basename = os.path.basename(input_file)
    input_file_rootname, _ = os.path.splitext(input_file_basename)
    metadata['date'] = "'%s'" % datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    if options.style_dir:
        metadata['staticDir'] = "'%s'" % options.style_dir.replace('\\', '/')
    if options.input:
        metadata['source_path'] = "'%s'" % input_file_dir
        metadata['source'] = "'%s'" % input_file_basename
        metadata['rootname'] = "'%s'" % input_file_rootname

    return metadata


def substitute_style_vars(parameters, options, style_vars_dict):

    # default style variables
    mapping = {
        'style_dir': options.style_dir,
        'panache_dir': script_dir,
        'build_date': "'%s'" % datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'input_dir': '',
        'input_basename': '',
        'input_basename_root': '',
        'input_basename_extension': '',
    }

    if options.input:
        input_dir = os.path.dirname(options.input)
        input_basename = os.path.basename(options.input)
        input_basename_root, input_basename_extension = os.path.splittext(input_basename)
        inputs_dict = {
            'input_dir': input_dir,
            'input_basename': input_basename,
            'input_basename_root': input_basename_root,
            'input_basename_extension': input_basename_extension,
        }
        mapping = {**mapping, **inputs_dict}

    # add style variables from the command line
    mapping = {**mapping, **style_vars_dict}

    yaml_dump = yaml.dump(parameters)
    template = Template(yaml_dump)
    substituted = template.safe_substitute(mapping)
    yaml_load = yaml.load(substituted)
    return yaml_load


def main():

    try:

        # parse and validate command line
        options, args, style_vars_dict = parse_cmdline(sys.argv[1:])

        # initialize styles from the data directory
        panache_styles = PanacheStyles()
        if options.style_dir:
            panache_styles.load(options.style_dir)

        # copy STDIN to a temporary file, iff needed
        input_file = options.input
        if not options.input:
            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(sys.stdin.buffer.read())
                input_file = f.name

        # load YAML from input (either the temporary file or the one name on the command line)
        input_yaml = get_input_yaml(input_file)

        # update (or add) style definitions based on definitions in the input file
        if input_yaml and STYLEDEF_ in input_yaml:
            for style_name in input_yaml[STYLEDEF_]:
                panache_styles.update(PanacheStyle(style_name, input_yaml[STYLEDEF_][style_name], input_file))

        # determine desired style
        style = determine_style(options, input_yaml)

        # resolve style to Pandoc compile parameters (and metadata)
        parameters = panache_styles.resolve(style)

        # substitute variables in the resolved style
        parameters = substitute_style_vars(parameters, options, style_vars_dict)

        # write the computed metadata to a temporary file
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write("---\n".encode())
            f.write(yaml.dump(parameters[METADATA_], encoding='utf-8'))
            f.write("---\n".encode())
            metadata_file = f.name

        # change to the directory containing the input, if not STDIN
        if options.input:
            os.chdir(os.path.dirname(options.input))

        # compile the command
        command = compile_command_line(input_file, metadata_file, parameters, options, args)

        # run the command
        process = Popen(command, stdout=sys.stdout, stderr=sys.stderr)
        process.wait()

        # delete the temporary files
        #silent_remove(metadata_file)
        if not options.input:
            silent_remove(input_file)

    except PanacheException as e:
        sys.stderr.write(e.message)
        sys.exit(e.code)


if __name__ == "__main__":
    main()