"""
    sphinx.builders.php
    ~~~~~~~~~~~~~~~~~~~~

    Several PHP builders.

    :copyright: Copyright 2007-2019 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

import html
import pickle
import posixpath
import re
import sys
import types
import warnings
from hashlib import md5
from os import path

import docutils
from docutils import nodes
from docutils.core import Publisher
from docutils.frontend import OptionParser
from docutils.io import DocTreeInput, StringOutput
from docutils.readers.doctree import Reader as DoctreeReader
from docutils.utils import relative_path

from sphinx import package_dir, __display_version__
from sphinx.application import ENV_PICKLE_FILENAME
from sphinx.builders import Builder
from sphinx.deprecation import (
    RemovedInSphinx30Warning, RemovedInSphinx40Warning, deprecated_alias
)
from sphinx.environment.adapters.asset import ImageAdapter
from sphinx.environment.adapters.indexentries import IndexEntries
from sphinx.environment.adapters.toctree import TocTree
from sphinx.errors import ConfigError, ThemeError
from sphinx.highlighting import PygmentsBridge
from sphinx.locale import _, __
from sphinx.search import js_index
from sphinx.theming import HTMLThemeFactory
from sphinx.util import jsonimpl, logging, status_iterator
from sphinx.util.console import bold  # type: ignore
from sphinx.util.docutils import is_html5_writer_available, new_document
from sphinx.util.fileutil import copy_asset
from sphinx.util.i18n import format_date
from sphinx.util.inventory import InventoryFile
from sphinx.util.matching import patmatch, Matcher, DOTFILES
from sphinx.util.osutil import SEP, os_path, relative_uri, ensuredir, \
    movefile, copyfile
from sphinx.writers.html import HTMLWriter, HTMLTranslator

from sphinx.builders.html import get_stable_hash, Stylesheet, JSContainer, JavaScript, BuildInfo, \
    convert_html_css_files, convert_html_js_files, setup_js_tag_helper, validate_math_renderer

if False:
    # For type annotation
    from typing import Any, Dict, IO, Iterable, Iterator, List, Set, Type, Tuple  # NOQA
    from sphinx.application import Sphinx  # NOQA
    from sphinx.config import Config  # NOQA
    from sphinx.domains import Domain, Index, IndexEntry  # NOQA
    from sphinx.util.tags import Tags  # NOQA

#: the filename for the inventory of objects
INVENTORY_FILENAME = 'objects.inv'
#: the filename for the "last build" file (for serializing builders)
LAST_BUILD_FILENAME = 'last_build'

logger = logging.getLogger(__name__)
return_codes_re = re.compile('[\r\n]+')

class StandalonePHPBuilder(Builder):
    """
    Builds standalone PHP docs.
    """
    name = 'php'
    format = 'php'
    epilog = __('The PHP pages are in %(outdir)s.')

    copysource = True
    allow_parallel = True
    out_suffix = '.php'
    link_suffix = '.php'  # defaults to matching out_suffix
    indexer_format = js_index  # type: Any
    indexer_dumps_unicode = True
    # create links to original images from images [True/False]
    html_scaled_image_link = True
    supported_image_types = ['image/svg+xml', 'image/png',
                             'image/gif', 'image/jpeg']
    supported_remote_images = True
    supported_data_uri_images = True
    searchindex_filename = 'searchindex.js'
    add_permalinks = True
    allow_sharp_as_current_path = True
    embedded = False  # for things like PHP help or Qt help: suppresses sidebar
    search = True  # for things like PHP help and Apple help: suppress search
    use_index = False
    download_support = True  # enable download role
    # use html5 translator by default
    default_html5_translator = False

    imgpath = None          # type: str
    domain_indices = []     # type: List[Tuple[str, Type[Index], List[Tuple[str, List[IndexEntry]]], bool]]  # NOQA

    # cached publisher object for snippets
    _publisher = None

    def __init__(self, app):
        # type: (Sphinx) -> None
        super().__init__(app)

        # CSS files
        self.css_files = []  # type: List[Dict[str, str]]

        # JS files
        self.script_files = JSContainer()  # type: List[JavaScript]

    def init(self):
        # type: () -> None
        self.build_info = self.create_build_info()
        # basename of images directory
        self.imagedir = '_images'
        # section numbers for headings in the currently visited document
        self.secnumbers = {}  # type: Dict[str, Tuple[int, ...]]
        # currently written docname
        self.current_docname = None  # type: str

        self.init_templates()
        self.init_highlighter()
        self.init_css_files()
        self.init_js_files()

        html_file_suffix = self.get_builder_config('file_suffix', 'html')
        if html_file_suffix is not None:
            self.out_suffix = html_file_suffix

        html_link_suffix = self.get_builder_config('link_suffix', 'html')
        if html_link_suffix is not None:
            self.link_suffix = html_link_suffix
        else:
            self.link_suffix = self.out_suffix

        self.use_index = self.get_builder_config('use_index', 'html')

    def create_build_info(self):
        # type: () -> BuildInfo
        return BuildInfo(self.config, self.tags, ['html'])

    def _get_translations_js(self):
        # type: () -> str
        candidates = [path.join(dir, self.config.language,
                                'LC_MESSAGES', 'sphinx.js')
                      for dir in self.config.locale_dirs] + \
                     [path.join(package_dir, 'locale', self.config.language,
                                'LC_MESSAGES', 'sphinx.js'),
                      path.join(sys.prefix, 'share/sphinx/locale',
                                self.config.language, 'sphinx.js')]

        for jsfile in candidates:
            if path.isfile(jsfile):
                return jsfile
        return None

    def get_theme_config(self):
        # type: () -> Tuple[str, Dict]
        return self.config.html_theme, self.config.html_theme_options

    def init_templates(self):
        # type: () -> None
        theme_factory = HTMLThemeFactory(self.app)
        themename, themeoptions = self.get_theme_config()
        self.theme = theme_factory.create(themename)
        self.theme_options = themeoptions.copy()
        self.create_template_bridge()
        self.templates.init(self, self.theme)

    def init_highlighter(self):
        # type: () -> None
        # determine Pygments style and create the highlighter
        if self.config.pygments_style is not None:
            style = self.config.pygments_style
        elif self.theme:
            style = self.theme.get_config('theme', 'pygments_style', 'none')
        else:
            style = 'sphinx'
        self.highlighter = PygmentsBridge('html', style)

    def init_css_files(self):
        # type: () -> None
        for filename, attrs in self.app.registry.css_files:
            self.add_css_file(filename, **attrs)

        for filename, attrs in self.get_builder_config('css_files', 'html'):
            self.add_css_file(filename, **attrs)

    def add_css_file(self, filename, **kwargs):
        # type: (str, **str) -> None
        if '://' not in filename:
            filename = posixpath.join('_static', filename)

        self.css_files.append(Stylesheet(filename, **kwargs))  # type: ignore

    def init_js_files(self):
        # type: () -> None
        self.add_js_file('jquery.js')
        self.add_js_file('underscore.js')
        self.add_js_file('doctools.js')
        self.add_js_file('language_data.js')

        for filename, attrs in self.app.registry.js_files:
            self.add_js_file(filename, **attrs)

        for filename, attrs in self.get_builder_config('js_files', 'html'):
            self.add_js_file(filename, **attrs)

        if self.config.language and self._get_translations_js():
            self.add_js_file('translations.js')

    def add_js_file(self, filename, **kwargs):
        # type: (str, **str) -> None
        if filename and '://' not in filename:
            filename = posixpath.join('_static', filename)

        self.script_files.append(JavaScript(filename, **kwargs))

    @property
    def default_translator_class(self):  # type: ignore
        # Use HTMLTranslator for PHP builder
        return HTMLTranslator
    
    @property
    def math_renderer_name(self):
        # type: () -> str
        name = self.get_builder_config('math_renderer', 'html')
        if name is not None:
            # use given name
            return name
        else:
            # not given: choose a math_renderer from registered ones as possible
            renderers = list(self.app.registry.html_inline_math_renderers)
            if len(renderers) == 1:
                # only default math_renderer (mathjax) is registered
                return renderers[0]
            elif len(renderers) == 2:
                # default and another math_renderer are registered; prior the another
                renderers.remove('mathjax')
                return renderers[0]
            else:
                # many math_renderers are registered. can't choose automatically!
                return None

    def get_outdated_docs(self):
        # type: () -> Iterator[str]
        try:
            with open(path.join(self.outdir, '.buildinfo')) as fp:
                buildinfo = BuildInfo.load(fp)

            if self.build_info != buildinfo:
                yield from self.env.found_docs
                return
        except ValueError as exc:
            logger.warning(__('Failed to read build info file: %r'), exc)
        except OSError:
            # ignore errors on reading
            pass

        if self.templates:
            template_mtime = self.templates.newest_template_mtime()
        else:
            template_mtime = 0
        for docname in self.env.found_docs:
            if docname not in self.env.all_docs:
                yield docname
                continue
            targetname = self.get_outfilename(docname)
            try:
                targetmtime = path.getmtime(targetname)
            except Exception:
                targetmtime = 0
            try:
                srcmtime = max(path.getmtime(self.env.doc2path(docname)),
                               template_mtime)
                if srcmtime > targetmtime:
                    yield docname
            except OSError:
                # source doesn't exist anymore
                pass

    def get_asset_paths(self):
        # type: () -> List[str]
        return self.config.html_extra_path + self.config.html_static_path

    def render_partial(self, node):
        # type: (nodes.Node) -> Dict[str, str]
        """Utility: Render a lone doctree node."""
        if node is None:
            return {'fragment': ''}
        doc = new_document('<partial node>')
        doc.append(node)

        if self._publisher is None:
            self._publisher = Publisher(
                source_class = DocTreeInput,
                destination_class=StringOutput)
            self._publisher.set_components('standalone',
                                           'restructuredtext', 'pseudoxml')

        pub = self._publisher

        pub.reader = DoctreeReader()
        pub.writer = HTMLWriter(self)
        pub.process_programmatic_settings(
            None, {'output_encoding': 'unicode'}, None)
        pub.set_source(doc, None)
        pub.set_destination(None, None)
        pub.publish()
        return pub.writer.parts

    def prepare_writing(self, docnames):
        # type: (Set[str]) -> None
        # create the search indexer
        self.indexer = None
        if self.search:
            from sphinx.search import IndexBuilder
            lang = self.config.html_search_language or self.config.language
            if not lang:
                lang = 'en'
            self.indexer = IndexBuilder(self.env, lang,
                                        self.config.html_search_options,
                                        self.config.html_search_scorer)
            self.load_indexer(docnames)

        self.docwriter = HTMLWriter(self)
        self.docsettings = OptionParser(
            defaults=self.env.settings,
            components=(self.docwriter,),
            read_config_files=True).get_default_values()  # type: Any
        self.docsettings.compact_lists = bool(self.config.html_compact_lists)

        # determine the additional indices to include
        self.domain_indices = []
        # html_domain_indices can be False/True or a list of index names
        indices_config = self.config.html_domain_indices
        if indices_config:
            for domain_name in sorted(self.env.domains):
                domain = None  # type: Domain
                domain = self.env.domains[domain_name]
                for indexcls in domain.indices:
                    indexname = '%s-%s' % (domain.name, indexcls.name)
                    if isinstance(indices_config, list):
                        if indexname not in indices_config:
                            continue
                    content, collapse = indexcls(domain).generate()
                    if content:
                        self.domain_indices.append(
                            (indexname, indexcls, content, collapse))

        # format the "last updated on" string, only once is enough since it
        # typically doesn't include the time of day
        lufmt = self.config.html_last_updated_fmt
        if lufmt is not None:
            self.last_updated = format_date(lufmt or _('%b %d, %Y'),
                                            language=self.config.language)
        else:
            self.last_updated = None

        logo = self.config.html_logo and \
            path.basename(self.config.html_logo) or ''

        favicon = self.config.html_favicon and \
            path.basename(self.config.html_favicon) or ''

        if not isinstance(self.config.html_use_opensearch, str):
            logger.warning(__('html_use_opensearch config value must now be a string'))

        self.relations = self.env.collect_relations()

        rellinks = []  # type: List[Tuple[str, str, str, str]]
        if self.use_index:
            rellinks.append(('genindex', _('General Index'), 'I', _('index')))
        for indexname, indexcls, content, collapse in self.domain_indices:
            # if it has a short name
            if indexcls.shortname:
                rellinks.append((indexname, indexcls.localname,
                                 '', indexcls.shortname))

        if self.config.html_style is not None:
            stylename = self.config.html_style
        elif self.theme:
            stylename = self.theme.get_config('theme', 'stylesheet')
        else:
            stylename = 'default.css'

        self.globalcontext = {
            'embedded': self.embedded,
            'project': self.config.project,
            'release': return_codes_re.sub('', self.config.release),
            'version': self.config.version,
            'last_updated': self.last_updated,
            'copyright': self.config.copyright,
            'master_doc': self.config.master_doc,
            'use_opensearch': self.config.html_use_opensearch,
            'docstitle': self.config.html_title,
            'shorttitle': self.config.html_short_title,
            'show_copyright': self.config.html_show_copyright,
            'show_sphinx': self.config.html_show_sphinx,
            'has_source': self.config.html_copy_source,
            'show_source': self.config.html_show_sourcelink,
            'sourcelink_suffix': self.config.html_sourcelink_suffix,
            'file_suffix': self.out_suffix,
            'script_files': self.script_files,
            'language': self.config.language,
            'css_files': self.css_files,
            'sphinx_version': __display_version__,
            'style': stylename,
            'rellinks': rellinks,
            'builder': self.name,
            'parents': [],
            'logo': logo,
            'favicon': favicon
        }
        if self.theme:
            self.globalcontext.update(
                ('theme_' + key, val) for (key, val) in
                self.theme.get_options(self.theme_options).items())
        self.globalcontext.update(self.config.html_context)

    def get_doc_context(self, docname, body, metatags):
        # type: (str, str, str) -> Dict[str, Any]
        """Collect items for the template context of a page."""
        # find out relations
        prev = next = None
        parents = []
        rellinks = self.globalcontext['rellinks'][:]
        related = self.relations.get(docname)
        titles = self.env.titles
        if related and related[2]:
            try:
                next = {
                    'link': self.get_relative_uri(docname, related[2]),
                    'title': self.render_partial(titles[related[2]])['title']
                }
                rellinks.append((related[2], next['title'], 'N', _('next')))
            except KeyError:
                next = None
        if related and related[1]:
            try:
                prev = {
                    'link': self.get_relative_uri(docname, related[1]),
                    'title': self.render_partial(titles[related[1]])['title']
                }
                rellinks.append((related[1], prev['title'], 'P', _('previous')))
            except KeyError:
                # the relation is (somehow) not in the TOC tree, handle
                # that gracefully
                prev = None
        while related and related[0]:
            try:
                parents.append(
                    {'link': self.get_relative_uri(docname, related[0]),
                     'title': self.render_partial(titles[related[0]])['title']})
            except KeyError:
                pass
            related = self.relations.get(related[0])
        if parents:
            # remove link to the master file; we have a generic
            # "back to index" link already
            parents.pop()
        parents.reverse()

        # title rendered as HTML
        title_node = self.env.longtitles.get(docname)
        title = title_node and self.render_partial(title_node)['title'] or ''

        # Suffix for the document
        source_suffix = path.splitext(self.env.doc2path(docname))[1]

        # the name for the copied source
        if self.config.html_copy_source:
            sourcename = docname + source_suffix
            if source_suffix != self.config.html_sourcelink_suffix:
                sourcename += self.config.html_sourcelink_suffix
        else:
            sourcename = ''

        # metadata for the document
        meta = self.env.metadata.get(docname)

        # local TOC and global TOC tree
        self_toc = TocTree(self.env).get_toc_for(docname, self)
        toc = self.render_partial(self_toc)['fragment']

        return {
            'parents': parents,
            'prev': prev,
            'next': next,
            'title': title,
            'meta': meta,
            'body': body,
            'metatags': metatags,
            'rellinks': rellinks,
            'sourcename': sourcename,
            'toc': toc,
            # only display a TOC if there's more than one item to show
            'display_toc': (self.env.toc_num_entries[docname] > 1),
            'page_source_suffix': source_suffix,
        }

    def write_doc(self, docname, doctree):
        # type: (str, nodes.document) -> None
        destination = StringOutput(encoding='utf-8')
        doctree.settings = self.docsettings

        self.secnumbers = self.env.toc_secnumbers.get(docname, {})
        self.fignumbers = self.env.toc_fignumbers.get(docname, {})
        self.imgpath = relative_uri(self.get_target_uri(docname), '_images')
        self.dlpath = relative_uri(self.get_target_uri(docname), '_downloads')
        self.current_docname = docname
        self.docwriter.write(doctree, destination)
        self.docwriter.assemble_parts()
        body = self.docwriter.parts['fragment']
        metatags = self.docwriter.clean_meta

        ctx = self.get_doc_context(docname, body, metatags)
        self.handle_page(docname, ctx, event_arg=doctree)

    def write_doc_serialized(self, docname, doctree):
        # type: (str, nodes.document) -> None
        self.imgpath = relative_uri(self.get_target_uri(docname), self.imagedir)
        self.post_process_images(doctree)
        title_node = self.env.longtitles.get(docname)
        title = title_node and self.render_partial(title_node)['title'] or ''
        self.index_page(docname, doctree, title)

    def finish(self):
        # type: () -> None
        self.finish_tasks.add_task(self.gen_indices)
        self.finish_tasks.add_task(self.gen_additional_pages)
        self.finish_tasks.add_task(self.copy_image_files)
        self.finish_tasks.add_task(self.copy_download_files)
        self.finish_tasks.add_task(self.copy_static_files)
        self.finish_tasks.add_task(self.copy_custom_pages)
        self.finish_tasks.add_task(self.copy_extra_files)
        self.finish_tasks.add_task(self.write_buildinfo)

        # dump the search index
        self.handle_finish()

    def gen_indices(self):
        # type: () -> None
        logger.info(bold(__('generating indices...')), nonl=1)

        # the global general index
        if self.use_index:
            self.write_genindex()

        # the global domain-specific indices
        self.write_domain_indices()

        logger.info('')

    def gen_additional_pages(self):
        # type: () -> None
        # pages from extensions
        for pagelist in self.app.emit('html-collect-pages'):
            for pagename, context, template in pagelist:
                self.handle_page(pagename, context, template)

        logger.info(bold(__('writing additional pages...')), nonl=1)

        # additional pages from conf.py
        for pagename, template in self.config.html_additional_pages.items():
            logger.info(' ' + pagename, nonl=1)
            self.handle_page(pagename, {}, template)

        # the search page
        if self.search:
            logger.info(' search', nonl=1)
            self.handle_page('search', {}, 'search.html')

        # the opensearch xml file
        if self.config.html_use_opensearch and self.search:
            logger.info(' opensearch', nonl=1)
            fn = path.join(self.outdir, '_static', 'opensearch.xml')
            self.handle_page('opensearch', {}, 'opensearch.xml', outfilename=fn)

        logger.info('')

    def write_genindex(self):
        # type: () -> None
        # the total count of lines for each index letter, used to distribute
        # the entries into two columns
        genindex = IndexEntries(self.env).create_index(self)
        indexcounts = []
        for _k, entries in genindex:
            indexcounts.append(sum(1 + len(subitems)
                                   for _, (_, subitems, _) in entries))

        genindexcontext = {
            'genindexentries': genindex,
            'genindexcounts': indexcounts,
            'split_index': self.config.html_split_index,
        }
        logger.info(' genindex', nonl=1)

        if self.config.html_split_index:
            self.handle_page('genindex', genindexcontext,
                             'genindex-split.html')
            self.handle_page('genindex-all', genindexcontext,
                             'genindex.html')
            for (key, entries), count in zip(genindex, indexcounts):
                ctx = {'key': key, 'entries': entries, 'count': count,
                       'genindexentries': genindex}
                self.handle_page('genindex-' + key, ctx,
                                 'genindex-single.html')
        else:
            self.handle_page('genindex', genindexcontext, 'genindex.html')

    def write_domain_indices(self):
        # type: () -> None
        for indexname, indexcls, content, collapse in self.domain_indices:
            indexcontext = {
                'indextitle': indexcls.localname,
                'content': content,
                'collapse_index': collapse,
            }
            logger.info(' ' + indexname, nonl=1)
            self.handle_page(indexname, indexcontext, 'domainindex.html')

    def copy_image_files(self):
        # type: () -> None
        if self.images:
            stringify_func = ImageAdapter(self.app.env).get_original_image_uri
            ensuredir(path.join(self.outdir, self.imagedir))
            for src in status_iterator(self.images, __('copying images... '), "brown",
                                       len(self.images), self.app.verbosity,
                                       stringify_func=stringify_func):
                dest = self.images[src]
                try:
                    copyfile(path.join(self.srcdir, src),
                             path.join(self.outdir, self.imagedir, dest))
                except Exception as err:
                    logger.warning(__('cannot copy image file %r: %s'),
                                   path.join(self.srcdir, src), err)

    def copy_download_files(self):
        # type: () -> None
        def to_relpath(f):
            # type: (str) -> str
            return relative_path(self.srcdir, f)
        # copy downloadable files
        if self.env.dlfiles:
            ensuredir(path.join(self.outdir, '_downloads'))
            for src in status_iterator(self.env.dlfiles, __('copying downloadable files... '),
                                       "brown", len(self.env.dlfiles), self.app.verbosity,
                                       stringify_func=to_relpath):
                try:
                    dest = path.join(self.outdir, '_downloads', self.env.dlfiles[src][1])
                    ensuredir(path.dirname(dest))
                    copyfile(path.join(self.srcdir, src), dest)
                except OSError as err:
                    logger.warning(__('cannot copy downloadable file %r: %s'),
                                   path.join(self.srcdir, src), err)

    def copy_custom_pages(self):
         # then, copy over all user-supplied static files
        excluded = Matcher(self.config.exclude_patterns + ["**/.*"])
        for static_path in self.config.html_static_path:
            entry = path.join(self.confdir, static_path)
            if not path.exists(entry):
                logger.warning(__('html_static_path entry %r does not exist'), entry)
                continue
            copy_asset(entry, path.join(self.outdir, '_static'), excluded,
                        context=ctx, renderer=self.templates)


    def copy_static_files(self):
        # type: () -> None
        try:
            # copy static files
            logger.info(bold(__('copying static files... ')), nonl=True)
            ensuredir(path.join(self.outdir, '_static'))
            # first, create pygments style file
            with open(path.join(self.outdir, '_static', 'pygments.css'), 'w') as f:
                f.write(self.highlighter.get_stylesheet())
            # then, copy translations JavaScript file
            if self.config.language is not None:
                jsfile = self._get_translations_js()
                if jsfile:
                    copyfile(jsfile, path.join(self.outdir, '_static',
                                               'translations.js'))

            # copy non-minified stemmer JavaScript file
            if self.indexer is not None:
                jsfile = self.indexer.get_js_stemmer_rawcode()
                if jsfile:
                    copyfile(jsfile, path.join(self.outdir, '_static', '_stemmer.js'))

            ctx = self.globalcontext.copy()

            # add context items for search function used in searchtools.js_t
            if self.indexer is not None:
                ctx.update(self.indexer.context_for_searchtool())

            # then, copy over theme-supplied static files
            if self.theme:
                for theme_path in self.theme.get_theme_dirs()[::-1]:
                    entry = path.join(theme_path, 'static')
                    copy_asset(entry, path.join(self.outdir, '_static'), excluded=DOTFILES,
                               context=ctx, renderer=self.templates)
            # then, copy over all user-supplied static files
            excluded = Matcher(self.config.exclude_patterns + ["**/.*"])
            for static_path in self.config.html_static_path:
                entry = path.join(self.confdir, static_path)
                if not path.exists(entry):
                    logger.warning(__('html_static_path entry %r does not exist'), entry)
                    continue
                copy_asset(entry, path.join(self.outdir, '_static'), excluded,
                           context=ctx, renderer=self.templates)
            # copy logo and favicon files if not already in static path
            if self.config.html_logo:
                logobase = path.basename(self.config.html_logo)
                logotarget = path.join(self.outdir, '_static', logobase)
                if not path.isfile(path.join(self.confdir, self.config.html_logo)):
                    logger.warning(__('logo file %r does not exist'), self.config.html_logo)
                elif not path.isfile(logotarget):
                    copyfile(path.join(self.confdir, self.config.html_logo),
                             logotarget)
            if self.config.html_favicon:
                iconbase = path.basename(self.config.html_favicon)
                icontarget = path.join(self.outdir, '_static', iconbase)
                if not path.isfile(path.join(self.confdir, self.config.html_favicon)):
                    logger.warning(__('favicon file %r does not exist'),
                                   self.config.html_favicon)
                elif not path.isfile(icontarget):
                    copyfile(path.join(self.confdir, self.config.html_favicon),
                             icontarget)
            logger.info(__('done'))
        except OSError as err:
            logger.warning(__('cannot copy static file %r'), err)

    def copy_extra_files(self):
        # type: () -> None
        try:
            # copy html_extra_path files
            logger.info(bold(__('copying extra files... ')), nonl=True)
            excluded = Matcher(self.config.exclude_patterns)

            for extra_path in self.config.html_extra_path:
                entry = path.join(self.confdir, extra_path)
                if not path.exists(entry):
                    logger.warning(__('html_extra_path entry %r does not exist'), entry)
                    continue

                copy_asset(entry, self.outdir, excluded)
            logger.info(__('done'))
        except OSError as err:
            logger.warning(__('cannot copy extra file %r'), err)

    def write_buildinfo(self):
        # type: () -> None
        try:
            with open(path.join(self.outdir, '.buildinfo'), 'w') as fp:
                self.build_info.dump(fp)
        except OSError as exc:
            logger.warning(__('Failed to write build info file: %r'), exc)

    def cleanup(self):
        # type: () -> None
        # clean up theme stuff
        if self.theme:
            self.theme.cleanup()

    def post_process_images(self, doctree):
        # type: (nodes.Node) -> None
        """Pick the best candidate for an image and link down-scaled images to
        their high res version.
        """
        Builder.post_process_images(self, doctree)

        if self.config.html_scaled_image_link and self.html_scaled_image_link:
            for node in doctree.traverse(nodes.image):
                scale_keys = ('scale', 'width', 'height')
                if not any((key in node) for key in scale_keys) or \
                   isinstance(node.parent, nodes.reference):
                    # docutils does unfortunately not preserve the
                    # ``target`` attribute on images, so we need to check
                    # the parent node here.
                    continue
                uri = node['uri']
                reference = nodes.reference('', '', internal=True)
                if uri in self.images:
                    reference['refuri'] = posixpath.join(self.imgpath,
                                                         self.images[uri])
                else:
                    reference['refuri'] = uri
                node.replace_self(reference)
                reference.append(node)

    def load_indexer(self, docnames):
        # type: (Iterable[str]) -> None
        keep = set(self.env.all_docs) - set(docnames)
        try:
            searchindexfn = path.join(self.outdir, self.searchindex_filename)
            if self.indexer_dumps_unicode:
                with open(searchindexfn, encoding='utf-8') as ft:
                    self.indexer.load(ft, self.indexer_format)
            else:
                with open(searchindexfn, 'rb') as fb:
                    self.indexer.load(fb, self.indexer_format)
        except (OSError, ValueError):
            if keep:
                logger.warning(__('search index couldn\'t be loaded, but not all '
                                  'documents will be built: the index will be '
                                  'incomplete.'))
        # delete all entries for files that will be rebuilt
        self.indexer.prune(keep)

    def index_page(self, pagename, doctree, title):
        # type: (str, nodes.document, str) -> None
        # only index pages with title
        if self.indexer is not None and title:
            filename = self.env.doc2path(pagename, base=None)
            try:
                self.indexer.feed(pagename, filename, title, doctree)
            except TypeError:
                # fallback for old search-adapters
                self.indexer.feed(pagename, title, doctree)  # type: ignore
                indexer_name = self.indexer.__class__.__name__
                warnings.warn(
                    'The %s.feed() method signature is deprecated. Update to '
                    '%s.feed(docname, filename, title, doctree).' % (
                        indexer_name, indexer_name),
                    RemovedInSphinx40Warning)

    def _get_local_toctree(self, docname, collapse=True, **kwds):
        # type: (str, bool, Any) -> str
        if 'includehidden' not in kwds:
            kwds['includehidden'] = False
        return self.render_partial(TocTree(self.env).get_toctree_for(
            docname, self, collapse, **kwds))['fragment']

    def get_outfilename(self, pagename):
        # type: (str) -> str
        return path.join(self.outdir, os_path(pagename) + self.out_suffix)

    def add_sidebars(self, pagename, ctx):
        # type: (str, Dict) -> None
        def has_wildcard(pattern):
            # type: (str) -> bool
            return any(char in pattern for char in '*?[')
        sidebars = None
        matched = None
        customsidebar = None

        # default sidebars settings for selected theme
        if self.theme.name == 'alabaster':
            # provide default settings for alabaster (for compatibility)
            # Note: this will be removed before Sphinx-2.0
            try:
                # get default sidebars settings from alabaster (if defined)
                theme_default_sidebars = self.theme.config.get('theme', 'sidebars')
                if theme_default_sidebars:
                    sidebars = [name.strip() for name in theme_default_sidebars.split(',')]
            except Exception:
                # fallback to better default settings
                sidebars = ['about.html', 'navigation.html', 'relations.html',
                            'searchbox.html', 'donate.html']
        else:
            theme_default_sidebars = self.theme.get_config('theme', 'sidebars', None)
            if theme_default_sidebars:
                sidebars = [name.strip() for name in theme_default_sidebars.split(',')]

        # user sidebar settings
        html_sidebars = self.get_builder_config('sidebars', 'html')
        for pattern, patsidebars in html_sidebars.items():
            if patmatch(pagename, pattern):
                if matched:
                    if has_wildcard(pattern):
                        # warn if both patterns contain wildcards
                        if has_wildcard(matched):
                            logger.warning(__('page %s matches two patterns in '
                                              'html_sidebars: %r and %r'),
                                           pagename, matched, pattern)
                        # else the already matched pattern is more specific
                        # than the present one, because it contains no wildcard
                        continue
                matched = pattern
                sidebars = patsidebars

        if sidebars is None:
            # keep defaults
            pass

        ctx['sidebars'] = sidebars
        ctx['customsidebar'] = customsidebar

    # --------- these are overwritten by the serialization builder

    def get_target_uri(self, docname, typ=None):
        # type: (str, str) -> str
        return docname + self.link_suffix

    def handle_page(self, pagename, addctx, templatename='page.html',
                    outfilename=None, event_arg=None):
        # type: (str, Dict, str, str, Any) -> None
        ctx = self.globalcontext.copy()
        # current_page_name is backwards compatibility
        ctx['pagename'] = ctx['current_page_name'] = pagename
        ctx['encoding'] = self.config.html_output_encoding
        default_baseuri = self.get_target_uri(pagename)
        # in the singlehtml builder, default_baseuri still contains an #anchor
        # part, which relative_uri doesn't really like...
        default_baseuri = default_baseuri.rsplit('#', 1)[0]

        if self.config.html_baseurl:
            ctx['pageurl'] = posixpath.join(self.config.html_baseurl,
                                            pagename + self.out_suffix)
        else:
            ctx['pageurl'] = None

        def pathto(otheruri, resource=False, baseuri=default_baseuri):
            # type: (str, bool, str) -> str
            if resource and '://' in otheruri:
                # allow non-local resources given by scheme
                return otheruri
            elif not resource:
                otheruri = self.get_target_uri(otheruri)
            uri = relative_uri(baseuri, otheruri) or '#'
            if uri == '#' and not self.allow_sharp_as_current_path:
                uri = baseuri
            return uri
        ctx['pathto'] = pathto

        def css_tag(css):
            # type: (Stylesheet) -> str
            attrs = []
            for key in sorted(css.attributes):
                value = css.attributes[key]
                if value is not None:
                    attrs.append('%s="%s"' % (key, html.escape(value, True)))
            attrs.append('href="%s"' % pathto(css.filename, resource=True))
            return '<link %s />' % ' '.join(attrs)
        ctx['css_tag'] = css_tag

        def hasdoc(name):
            # type: (str) -> bool
            if name in self.env.all_docs:
                return True
            elif name == 'search' and self.search:
                return True
            elif name == 'genindex' and self.get_builder_config('use_index', 'html'):
                return True
            return False
        ctx['hasdoc'] = hasdoc

        def warn(*args, **kwargs):
            # type: (Any, Any) -> str
            """Simple warn() wrapper for themes."""
            warnings.warn('The template function warn() was deprecated. '
                          'Use warning() instead.',
                          RemovedInSphinx30Warning, stacklevel=2)
            logger.warning(*args, **kwargs)
            return ''  # return empty string
        ctx['warn'] = warn

        ctx['toctree'] = lambda **kw: self._get_local_toctree(pagename, **kw)
        self.add_sidebars(pagename, ctx)
        ctx.update(addctx)

        self.update_page_context(pagename, templatename, ctx, event_arg)
        newtmpl = self.app.emit_firstresult('html-page-context', pagename,
                                            templatename, ctx, event_arg)
        if newtmpl:
            templatename = newtmpl

        try:
            output = self.templates.render(templatename, ctx)
        except UnicodeError:
            logger.warning(__("a Unicode error occurred when rendering the page %s. "
                              "Please make sure all config values that contain "
                              "non-ASCII content are Unicode strings."), pagename)
            return
        except Exception as exc:
            raise ThemeError(__("An error happened in rendering the page %s.\nReason: %r") %
                             (pagename, exc))

        if not outfilename:
            outfilename = self.get_outfilename(pagename)
        # outfilename's path is in general different from self.outdir
        ensuredir(path.dirname(outfilename))
        try:
            with open(outfilename, 'w', encoding=ctx['encoding'],
                      errors='xmlcharrefreplace') as f:
                f.write(output)
        except OSError as err:
            logger.warning(__("error writing file %s: %s"), outfilename, err)
        if self.copysource and ctx.get('sourcename'):
            # copy the source file for the "show source" link
            source_name = path.join(self.outdir, '_sources',
                                    os_path(ctx['sourcename']))
            ensuredir(path.dirname(source_name))
            copyfile(self.env.doc2path(pagename), source_name)

    def update_page_context(self, pagename, templatename, ctx, event_arg):
        # type: (str, str, Dict, Any) -> None
        pass

    def handle_finish(self):
        # type: () -> None
        if self.indexer:
            self.finish_tasks.add_task(self.dump_search_index)
        self.finish_tasks.add_task(self.dump_inventory)

    def dump_inventory(self):
        # type: () -> None
        logger.info(bold(__('dumping object inventory... ')), nonl=True)
        InventoryFile.dump(path.join(self.outdir, INVENTORY_FILENAME), self.env, self)
        logger.info(__('done'))

    def dump_search_index(self):
        # type: () -> None
        logger.info(
            bold(__('dumping search index in %s ... ') % self.indexer.label()),
            nonl=True)
        self.indexer.prune(self.env.all_docs)
        searchindexfn = path.join(self.outdir, self.searchindex_filename)
        # first write to a temporary file, so that if dumping fails,
        # the existing index won't be overwritten
        if self.indexer_dumps_unicode:
            with open(searchindexfn + '.tmp', 'w', encoding='utf-8') as ft:
                self.indexer.dump(ft, self.indexer_format)
        else:
            with open(searchindexfn + '.tmp', 'wb') as fb:
                self.indexer.dump(fb, self.indexer_format)
        movefile(searchindexfn + '.tmp', searchindexfn)
        logger.info(__('done'))


class DirectoryPHPBuilder(StandalonePHPBuilder):
    """
    A StandalonePHPBuilder that creates all PHP pages as "index.php" in
    a directory given by their pagename, so that generated URLs don't have
    ``.php`` in them.
    """
    name = 'dirphp'

    def get_target_uri(self, docname, typ=None):
        # type: (str, str) -> str
        if docname == 'index':
            return ''
        if docname.endswith(SEP + 'index'):
            return docname[:-5]  # up to sep
        return docname + SEP

    def get_outfilename(self, pagename):
        # type: (str) -> str
        if pagename == 'index' or pagename.endswith(SEP + 'index'):
            outfilename = path.join(self.outdir, os_path(pagename) +
                                    self.out_suffix)
        else:
            outfilename = path.join(self.outdir, os_path(pagename),
                                    'index' + self.out_suffix)

        return outfilename

    def prepare_writing(self, docnames):
        # type: (Set[str]) -> None
        super().prepare_writing(docnames)
        self.globalcontext['no_search_suffix'] = True


class SerializingPHPBuilder(StandalonePHPBuilder):
    """
    An abstract builder that serializes the generated PHP.
    """
    #: the serializing implementation to use.  Set this to a module that
    #: implements a `dump`, `load`, `dumps` and `loads` functions
    #: (pickle, simplejson etc.)
    implementation = None  # type: Any
    implementation_dumps_unicode = False
    #: additional arguments for dump()
    additional_dump_args = ()  # type: Tuple

    #: the filename for the global context file
    globalcontext_filename = None  # type: str

    supported_image_types = ['image/svg+xml', 'image/png',
                             'image/gif', 'image/jpeg']

    def init(self):
        # type: () -> None
        self.build_info = BuildInfo(self.config, self.tags)
        self.imagedir = '_images'
        self.current_docname = None
        self.theme = None       # no theme necessary
        self.templates = None   # no template bridge necessary
        self.init_templates()
        self.init_highlighter()
        self.init_css_files()
        self.init_js_files()
        self.use_index = self.get_builder_config('use_index', 'html')

    def get_target_uri(self, docname, typ=None):
        # type: (str, str) -> str
        if docname == 'index':
            return ''
        if docname.endswith(SEP + 'index'):
            return docname[:-5]  # up to sep
        return docname + SEP

    def dump_context(self, context, filename):
        # type: (Dict, str) -> None
        if self.implementation_dumps_unicode:
            with open(filename, 'w', encoding='utf-8') as ft:
                self.implementation.dump(context, ft, *self.additional_dump_args)
        else:
            with open(filename, 'wb') as fb:
                self.implementation.dump(context, fb, *self.additional_dump_args)

    def handle_page(self, pagename, ctx, templatename='page.html',
                    outfilename=None, event_arg=None):
        # type: (str, Dict, str, str, Any) -> None
        ctx['current_page_name'] = pagename
        self.add_sidebars(pagename, ctx)

        if not outfilename:
            outfilename = path.join(self.outdir,
                                    os_path(pagename) + self.out_suffix)

        # we're not taking the return value here, since no template is
        # actually rendered
        self.app.emit('html-page-context', pagename, templatename, ctx, event_arg)

        # make context object serializable
        for key in list(ctx):
            if isinstance(ctx[key], types.FunctionType):
                del ctx[key]

        ensuredir(path.dirname(outfilename))
        self.dump_context(ctx, outfilename)

        # if there is a source file, copy the source file for the
        # "show source" link
        if ctx.get('sourcename'):
            source_name = path.join(self.outdir, '_sources',
                                    os_path(ctx['sourcename']))
            ensuredir(path.dirname(source_name))
            copyfile(self.env.doc2path(pagename), source_name)

    def handle_finish(self):
        # type: () -> None
        # dump the global context
        outfilename = path.join(self.outdir, self.globalcontext_filename)
        self.dump_context(self.globalcontext, outfilename)

        # super here to dump the search index
        super().handle_finish()

        # copy the environment file from the doctree dir to the output dir
        # as needed by the web app
        copyfile(path.join(self.doctreedir, ENV_PICKLE_FILENAME),
                 path.join(self.outdir, ENV_PICKLE_FILENAME))

        # touch 'last build' file, used by the web application to determine
        # when to reload its environment and clear the cache
        open(path.join(self.outdir, LAST_BUILD_FILENAME), 'w').close()

def setup(app):
    # type: (Sphinx) -> Dict[str, Any]
    # builders
    app.add_builder(StandalonePHPBuilder)
    app.add_builder(DirectoryPHPBuilder)

    return {
        'version': 'builtin',
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }