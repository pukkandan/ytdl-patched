#!/usr/bin/env python3

# Allow direct execution
import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inspect import getsource

from devscripts.utils import get_filename_args, read_file, write_file

NO_ATTR = object()
STATIC_CLASS_PROPERTIES = [
    'IE_NAME', 'IE_DESC', 'SEARCH_KEY', '_VALID_URL', '_WORKING', '_ENABLED', '_NETRC_MACHINE', 'age_limit'
]
CLASS_METHODS = [
    'ie_key', 'working', 'description', 'suitable', '_match_valid_url', '_match_id', 'get_temp_id', 'is_suitable'
]
IE_TEMPLATE = '''
class {name}({bases}):
    _module = {module!r}
'''
MODULE_TEMPLATE = read_file('devscripts/lazy_load_template.py')


def main():
    lazy_extractors_filename = get_filename_args(default_outfile='yt_dlp/extractor/lazy_extractors.py')
    if os.path.exists(lazy_extractors_filename):
        os.remove(lazy_extractors_filename)

    _ALL_CLASSES = get_all_ies()  # Must be before import

    from yt_dlp.extractor.common import InfoExtractor, SearchInfoExtractor

    DummyInfoExtractor = type('InfoExtractor', (InfoExtractor,), {'IE_NAME': NO_ATTR})
    module_src = '\n'.join((
        MODULE_TEMPLATE,
        '    _module = None',
        *extra_ie_code(DummyInfoExtractor),
        '\nclass LazyLoadSearchExtractor(LazyLoadExtractor):\n    pass\n',
        *build_ies(_ALL_CLASSES, (InfoExtractor, SearchInfoExtractor), DummyInfoExtractor),
    ))

    write_file(lazy_extractors_filename, f'{module_src}\n')


def get_all_ies():
    PLUGINS_DIRNAME = 'ytdlp_plugins'
    BLOCKED_DIRNAME = f'{PLUGINS_DIRNAME}_blocked'
    if os.path.exists(PLUGINS_DIRNAME):
        os.rename(PLUGINS_DIRNAME, BLOCKED_DIRNAME)
    try:
        from yt_dlp.extractor.extractors import _ALL_CLASSES
    finally:
        if os.path.exists(BLOCKED_DIRNAME):
            os.rename(BLOCKED_DIRNAME, PLUGINS_DIRNAME)
    return _ALL_CLASSES


def extra_ie_code(ie, base=None):
    for var in STATIC_CLASS_PROPERTIES:
        val = getattr(ie, var)
        if val != (getattr(base, var) if base else NO_ATTR):
            yield f'    {var} = {val!r}'
    yield ''

    for name in CLASS_METHODS:
        f = getattr(ie, name)
        if not base or f.__func__ != getattr(base, name).__func__:
            yield getsource(f)


def cleanup_regex(regex_str):
    if not isinstance(regex_str, (str, bytes)):
        return regex_str
    has_extended = re.search(r'\(\?[aiLmsux]*x[aiLmsux]*\)', regex_str)  # something like (?xxs) may match, but (?s) or (?i) won't
    if not has_extended:
        return regex_str
    # remove comments
    regex_str = re.sub(r'(?m)\s+#.+?$', '', regex_str)
    # remove spaces and indents
    regex_str = re.sub(r'\s+', '', regex_str)
    # remove x (EXTENDED) from all inline flags
    regex_str = re.sub(r'\(\?([aiLmsux]+)\)', lambda m: '(?%s)' % m.group(1).replace('x', ''), regex_str)
    regex_str = re.sub(r'\(\?\)', '', regex_str)

    return regex_str


def build_ies(ies, bases, attr_base):
    names = []
    for ie in sort_ies(ies, bases):
        yield build_lazy_ie(ie, ie.__name__, attr_base)
        if ie in ies:
            names.append(ie.__name__)

    yield f'\n_ALL_CLASSES = [{", ".join(names)}]'


def sort_ies(ies, ignored_bases):
    """find the correct sorting and add the required base classes so that subclasses can be correctly created"""
    classes, returned_classes = ies[:-1], set()
    assert ies[-1].__name__ == 'GenericIE', 'Last IE must be GenericIE'
    while classes:
        for c in classes[:]:
            bases = set(c.__bases__) - {object, *ignored_bases}
            restart = False
            for b in sorted(bases, key=lambda x: x.__name__):
                if b not in classes and b not in returned_classes:
                    assert b.__name__ != 'GenericIE', 'Cannot inherit from GenericIE'
                    classes.insert(0, b)
                    restart = True
            if restart:
                break
            if bases <= returned_classes:
                yield c
                returned_classes.add(c)
                classes.remove(c)
                break
    yield ies[-1]


def build_lazy_ie(ie, name, attr_base):
    bases = ', '.join({
        'InfoExtractor': 'LazyLoadExtractor',
        'SearchInfoExtractor': 'LazyLoadSearchExtractor',
    }.get(base.__name__, base.__name__) for base in ie.__bases__)

    s = IE_TEMPLATE.format(name=name, module=ie.__module__, bases=bases)
    return s + '\n'.join(extra_ie_code(ie, attr_base))


if __name__ == '__main__':
    main()
