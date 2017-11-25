#!/usr/bin/env python3
"""
This script attempts to deduplicate entries in a dictionary or to detect whether
duplicates are present.
In the normal operating mode, the questionable entries are removed, the result
is written to a file-dedup.py and then it is converted to the C5 format. From
there, a diff with the original format is done. With the diff, the human in
front of this very computer can then reason about the changes.

There is also a mode where the script tries to find a duplicated translation and
exits upon the first match.

Dependencies: xsltproc, diff python >= 3.4.

The script tries to preserve the original XML, but may do the following changes:

-   Manual sense enumeration (using the attribute `n`) may be stripped.
    Because of the removal of doubled senses, the enumeration might be broken
    and it's automatically inserted by the output formatters, anyway.
-   XML declarations may get lost, feel free to add support for it. Comments
    work fine.
"""

import argparse
import io
import itertools
import os
import shlex
import shutil
import sys
import xml.etree.ElementTree as ET

# TEI name space, Python's parser doesn't handle them nicely
TEI_NS = '{http://www.tei-c.org/ns/1.0}'
# findall/iter with TEI namespace removed
findall = lambda x,y: x.findall(TEI_NS + y)
tei_iter = lambda x,y: x.iter(TEI_NS + y)

class HelpfulParser(argparse.ArgumentParser):
    """Unlike the super class, this arg parse instance will print the error it
    encountered as well as the complete usage of the program. It will also
    redirect the usage to a output formatter."""
    def __init__(self, name, description=None):
        super().__init__(prog=name, description=description)

    def error(self, message):
        """Print error message and usage information."""
        sys.stderr.write('Error: ' + message)
        self.print_help()
        sys.exit(2)


class CommentedTreeBuilder(ET.TreeBuilder):
    """A TreeBuilder subclass that retains XML comments from the source. It can
    be treated as a black box saving the contents before and after the root tag,
    so that it can be re-added when writing back a XML ElementTree to disk. This
    is necessary because of lxml/ElementTree's inability to handle declarations
    nicely."""
    def comment(self, data):
        self.start(ET.Comment, {})
        self.data(data)
        self.end(ET.Comment)


def rm_doubled_senses(entry):
    """Some entries have multiple senses. A few of them are exactly the same,
    remove these.
    This function returns True if an element has been altered"""
    senses = list(findall(entry, 'sense'))
    if len(senses) == 1:
        return
    # obtain a mapping from XML node -> list of words within `<quote>…</quote>`
    senses = {sense: tuple(q.text.strip() for q in tei_iter(sense, 'quote')
            if q.text) for sense in senses}
    changed = False
    # pair each sense with another and compare their content
    for s1, s2 in itertools.combinations(senses.items(), 2):
        if len(s1[1]) == len(s2[1]):
            # if two senses are *excactly* identical
            if all(e1 == e2 for e1, e2 in zip(s1[1], s2[1])):
                try:
                    entry.remove(s2[0]) # sense node object
                    changed = True
                except ValueError: # already removed?
                    pass
    return changed


def rm_empty_nodes(entry):
    """This function removes nodes which have no text and no children and are
    hence without semantic. It also resets the fixed counters on sense, since
    they are generated by the output formatter anyway.
    This function returns True, if an empty node has been removed."""
    changed = False
    # sometimes parent nodes are empty after their empty children have been
    # removed, so do this three times (won't work with deeper nestings…)
    for _ in range(0, 2):
        nodes = [(None, entry)]
        for parent, node in nodes:
            # strip manual enumeration, handled by output formatters and might
            # be wrong after node removal
            if node.tag.endswith('sense') and node.get('n'):
                del node.attrib['n']
                changed = True
            if (node.text is None or node.text.strip() == '') \
                    and len(node.getchildren()) == 0:
                parent.remove(node)
                changed = True
            else:
                nodes.extend((node, c) for c in node.getchildren())
    return changed

def rm_doubled_quotes(entry):
    """Some entries have doubled quotes (translations) within different senses.
    Remove the doubled quotes.
    This function return True, if the entry has been modified."""
    senses = list(findall(entry, 'sense'))
    # add quote elements
    senses = [(cit, q)  for s in senses for cit in findall(s, 'cit')
            for q in findall(cit, 'quote')]
    if len(senses) <= 1:
        return
    changed = False
    # pair each sense with another and compare their content
    for trans1, trans2 in itertools.combinations(senses, 2):
        # could have been removed already, so check:
        cit1, quote1 = trans1
        cit2, quote2 = trans2
        if not cit1.findall(quote1.tag) or not cit2.findall(quote2.tag) \
                and cit1 is not cit2:
            continue # one of them has been removed already
        # text of both quotes match, remove second quote
        if quote1.text == quote2.text:
            cit2.remove(quote2)
            changed = True
    return changed

def exec(command):
    """Execute a command or fail straight away."""
    ret = os.system(command)
    if ret:
        sys.stderr.write("Process exited with %i: %s" % (ret, command))
        sys.exit(ret)

#pylint: disable=too-few-public-methods
class XmlParserWrapper:
    """This thin wrapper guards the parsing process.  It manually finds the TEI
    element and copies everything before and afterwards *verbatim*. This is due
    to the inability of the ElementTree parser to handle multiple "root
    elements", for instance comments before or after the root node or '<!'
    declarations.
    """
    def __init__(self, file_name):
        with open(file_name, encoding='utf-8') as file:
            content = file.read()
        if not any(u in content for u in ('utf-8', 'utf8', 'UTF8', 'UTF-8')):
            raise ValueError("XML file is not encoded in UTF-8. Please recode "
                    "the file or extend this parser and XML writer.")
        tei_start = content.find('<TEI')
        if tei_start < 0:
            raise ValueError("Couldn't find string `<TEI` in the XML file.  Please extend this parser.")
        self.before_root = content[:tei_start]
        content = content[tei_start:]
        tei_end = content.find('</TEI>')
        if tei_end < 0:
            raise ValueError("Couldn't find `</TEI>` in the input file, please extend the parser.")
        tei_end += len('</TEI>')
        self.after_root = content[tei_end:]
        content = content[:tei_end]
        parser = ET.XMLParser(target = CommentedTreeBuilder())
        parser.feed(content)
        self.root = parser.close()

    def write(self, file_name):
        """Write the XML element tree to a file, with hopefully a very similar
        formatting as before."""
        tree = ET.ElementTree(self.root)
        in_mem = io.BytesIO()
        tree.write(in_mem, encoding="UTF-8")
        in_mem.seek(0)
        with open(file_name, 'wb') as file:
            file.write(self.before_root.encode('UTF-8'))
            file.write(in_mem.read())
            file.write(self.after_root.encode('UTF-8'))
            if not self.after_root.endswith('\n'):
                file.write(b'\n')


def main():
    parser = HelpfulParser("deduplicator", description=("Fnd and remove "
        "duplicated translations and empty TEI nodes"))
    parser.add_argument("-s", "--detect_changes", dest="detect_changes",
              help=("check whether duplicates or empty nodes can be detected "
                  "and exit with exit code 42 if the first change would "
                  "need to be made"),
                  action="store_true", default=False)
    parser.add_argument('dictionary_path', help='input TEI file', nargs="+")
    args = parser.parse_args(sys.argv[1:])

    # register TEI name space without prefix to dump the *same* XML file
    ET.register_namespace('', 'http://www.tei-c.org/ns/1.0')
    dictionary_path = args.dictionary_path[0]
    tree = XmlParserWrapper(dictionary_path)
    changed = False
    for entry in tei_iter(tree.root, 'entry'):
        changed1 = rm_doubled_senses(entry)
        changed2 = rm_doubled_quotes(entry)
        # the processing above might leave empty parent nodes, remove those
        changed3 = rm_empty_nodes(entry)
        if args.detect_changes and any((changed1, changed2, changed3)):
            print("Problems found, aborting as requested.")
            sys.exit(42)
        changed = any((changed, changed1, changed2, changed3))
    if changed:
        output_fn = os.path.join('build', 'dictd',
                dictionary_path.replace('.tei', '-dedup.tei'))
        tree.write(output_fn)
        # get a human-readable diff of the changes
        if not shutil.which('less'):
            print("Please install diff to get a diff of the changes that have been made.")
            sys.exit(0)
        c5 = lambda x: shlex.quote(x.replace('.tei', '.c5'))
        exec('xsltproc $FREEDICT_TOOLS/xsl/tei2c5.xsl %s > %s' % (output_fn,
            c5(output_fn)))
        # convert original dictionary to c5
        exec('make build-dictd')
        # execute diff without checking the return type
        os.system('diff -u build/dictd/%s %s' % (c5(dictionary_path), c5(output_fn)))
    else:
        print("Nothing changed, no action taken.")


if __name__ == '__main__':
    main()
