# -*- coding: utf-8 -*-
from DateTime import DateTime
from DateTime.DateTime import safelocaltime
from plone import api
from plone.app.contentlisting.interfaces import IContentListing
from plone.app.search.browser import Search, SortOption
from plone.registry.interfaces import IRegistry
from Products.CMFCore.utils import getToolByName
from Products.CMFPlone.browser.navtree import getNavigationRoot
from Products.CMFPlone.PloneBatch import Batch
from Products.PluginIndexes.DateIndex.DateIndex import DateIndex
from Products.ZCTextIndex.ParseTree import ParseError
from rer.sitesearch import sitesearchMessageFactory as _
from rer.sitesearch.browser.interfaces import IRerSiteSearch
from rer.sitesearch.interfaces import IRERSiteSearchSettings
from zope.annotation.interfaces import IAnnotations
from zope.component import queryUtility, getUtility
from zope.i18n import translate
from zope.interface import implements
from ZPublisher.HTTPRequest import record
from ZTUtils import make_query
import logging
import urllib2

try:
    from collective.solr.interfaces import ISolrConnectionConfig
    HAS_SOLR = True
except ImportError:
    HAS_SOLR = False

logger = logging.getLogger(__name__)
MULTISPACE = u'\u3000'.encode('utf-8')
EVER = DateTime('1970-01-03')


def quote_chars(s):
    # We need to quote parentheses when searching text indices
    if '(' in s:
        s = s.replace('(', '"("')
    if ')' in s:
        s = s.replace(')', '")"')
    if MULTISPACE in s:
        s = s.replace(MULTISPACE, ' ')
    return s


class RERSearch(Search):
    """
    """
    implements(IRerSiteSearch)

    def __init__(self, context, request):
        """
        """
        super(RERSearch, self).__init__(context, request)
        self.catalog = api.portal.get_tool(name='portal_catalog')
        self.tabs_order = self.getRegistryInfos('tabs_order')
        if not self.tabs_order:
            self.tabs_order = ('all')
        self.indexes_order = self.getRegistryInfos('indexes_order')

    def getDebugInfo(self):
        annotations = IAnnotations(self.request)
        queries = [urllib2.unquote(x) for x in annotations.get('solr.debug', [])]
        return queries

    @property
    def tabs_mapping(self):
        tabs_map = self.getRegistryInfos('tabs_mapping')
        tabs_dict = {'all': {'title': 'All'}}
        for tab in tabs_map:
            tab_title = tab.tab_title
            tab_id = tab_title.lower().replace(' ', '-')
            tabs_dict[tab_id] = {'title': tab_title, 'portal_types': tab.portal_types}
        return tabs_dict

    @property
    def types_mapping(self):
        tabs_map = self.getRegistryInfos('tabs_mapping')
        types_dict = {}
        for tab in tabs_map:
            tab_id = tab.tab_title.lower().replace(' ', '-')
            tab_types = tab.portal_types
            for portal_type in tab_types:
                types_dict[portal_type] = tab_id
        return types_dict

    @property
    def available_indexes(self):
        indexes_map = self.getRegistryInfos('available_indexes')
        indexes_dict = {}
        for index in indexes_map:
            indexes_dict[index.index] = index.index_title
        return indexes_dict

    @property
    def hidden_indexes(self):
        indexes_map = self.getRegistryInfos('hidden_indexes')
        indexes_dict = {}
        for index in indexes_map:
            indexes_dict[index.index] = index.index_title
        return indexes_dict

    def getRegistryInfos(self, registry_item):
        """
        Return a value stored in plone.app.registry.
        @param: registry_item
        """
        registry = queryUtility(IRegistry)
        settings = registry.forInterface(IRERSiteSearchSettings, check=False)
        if not settings:
            return None
        return getattr(settings, registry_item, None)

    @property
    def valid_keys(self):
        """
        """
        valid_keys = ['sort_on', 'sort_order', 'sort_limit', 'fq', 'fl', 'facet', 'filter_tab']
        hidden_indexes = self.hidden_indexes
        for index in hidden_indexes.keys():
            if index not in valid_keys:
                    valid_keys.append(index)
        return tuple(valid_keys)

    def splitSearchOptions(self, value):
        """
        This method returns key and value. If there isn't a value, return the key as value
        @param: value is a string that contain a key and a value divided by "pipe" character
        """
        key_info = value.split('|')
        if len(key_info) == 2:
            return {'id': key_info[0], 'title': key_info[1]}
        else:
            return {'id': key_info[0], 'title': key_info[0]}

    def getSelectedTab(self, tabs):
        """
        """
        tab_in_request = self.request.form.get('filter_tab', '')
        if tab_in_request:
            return tab_in_request
        else:
            for tab in self.tabs_order:
                if tab in tabs:
                    return tab
        return "all"

    def results(self, query=None, batch=True, b_size=20, b_start=0):
        """ Get properly wrapped search results from the catalog.
        Everything in Plone that performs searches should go through this view.
        'query' should be a dictionary of catalog parameters.
        """
        if query is None:
            query = {}
        query, validation_messages = self.filter_query(query)
        result = {}
        if query is None:
            return {}
        if self.searchWithSolr(query):
            result = self.solrResults(query=query, batch=batch, b_size=b_size, b_start=b_start)
        else:
            result = self.catalogResults(query=query, batch=batch, b_size=b_size, b_start=b_start)
        if validation_messages:
            result['validation_messages'] = validation_messages
        return result

    def searchWithSolr(self, query):
        """
        Check if c.solr is installed and active,
        and if required solr queries are in the query
        """
        if not HAS_SOLR:
            return False
        solr_config = getUtility(ISolrConnectionConfig)
        if not solr_config.active:
            return False
        for field in solr_config.required:
            if not query.get(field):
                return False
        #if is all set, return the value in sitesearch settings
        return self.getRegistryInfos('solr_search_enabled')

    def solrResults(self, query, batch=True, b_size=20, b_start=0):
        query['facet'] = 'true'
        indexes_list = self.available_indexes.keys()
        indexes_list.append('portal_type')
        query['facet_field'] = indexes_list
        # BBB: temporary
        solr_fq_default = solr_bq_default = None
        try:
            rer_internos = getToolByName(self.context, 'portal_properties').rer_internos
            solr_fq_default = rer_internos.solr_fq_default.strip()
            # solr_bq_default = rer_internos.solr_bq_default.strip()
        except:
            logger.exception('portal_properties/rer_internos/solr_fq_default or portal_properties/rer_internos/solr_bq_default not found')
        if solr_fq_default and not 'fq' in query:
            query['fq'] = solr_fq_default
        if solr_bq_default and not 'bq' in query:
            query['bq'] = solr_bq_default
        if batch:
            query['b_size'] = b_size
            query['b_start'] = b_start
        results = self.catalog(**query)
        res_dict = {}
        filtered_results = []
        res_dict = {'tot_results_len': results.actual_result_count,
                    'tabs': 'all'}
        global_facet_counts = getattr(results, 'facet_counts', None)
        if global_facet_counts:
            facets = global_facet_counts.get('facet_fields', {})
            res_dict['tabs'] = self.solrAvailableTabs(facets)
        active_tab = self.context.REQUEST.form.get('filter_tab')
        if active_tab:
            filtered_results = self.doFilteredSearch(active_tab, query)
        else:
            if self.tabs_order[0] != "all":
                for tab_id in self.tabs_order:
                    filtered_results = self.doFilteredSearch(tab_id, query)
                    if filtered_results:
                        break
        if filtered_results:
            facet_counts = getattr(filtered_results, 'facet_counts', None)
            results = IContentListing(filtered_results)
        else:
            facet_counts = getattr(results, 'facet_counts', None)
            results = IContentListing(results)
        if batch:
            results = Batch(results, b_size, b_start)
        res_dict['results'] = results
        if facet_counts:
            facets = facet_counts.get('facet_fields', {})
            res_dict['indexes_dict'] = self.solrFacetsFormatter(facets)
        return res_dict

    def solrAvailableTabs(self, facets):
        """
        """
        portal_types = facets.get('portal_type', [])
        types_mapping = self.types_mapping
        available_tabs = ['all']
        for portal_type in portal_types:
            tab_id = types_mapping.get(portal_type, '')
            if portal_types.get(portal_type) and tab_id and tab_id not in available_tabs:
                available_tabs.append(tab_id)
        return available_tabs

    def solrFacetsFormatter(self, facets):
        """
        """
        filter_dict = {}
        indexes_mapping = self.available_indexes
        for facet_id, facet_values in facets.items():
            if facet_id in indexes_mapping:
                filter_dict[facet_id] = {'title': indexes_mapping.get(facet_id, facet_id),
                                         'values': facet_values}
        return filter_dict

    def catalogResults(self, query, batch=True, b_size=20, b_start=0):
        try:
            results = self.catalog(**query)
        except ParseError:
            return {}
        res_dict = {}
        filtered_results = []
        res_dict = {'tot_results_len': results.actual_result_count}
        active_tab = self.context.REQUEST.form.get('filter_tab')
        if active_tab:
            filtered_results = self.doFilteredSearch(active_tab, query)
        else:
            if self.tabs_order[0] != "all":
                for tab_id in self.tabs_order:
                    filtered_results = self.doFilteredSearch(tab_id, query)
                    if filtered_results:
                        break
        filtered_infos, available_tabs = self.getFilterInfos(results, filtered_results)
        if filtered_results:
            results = IContentListing(filtered_results)
        else:
            results = IContentListing(results)
        if batch:
            results = Batch(results, b_size, b_start)
        res_dict['results'] = results
        if filtered_infos:
            res_dict['indexes_dict'] = filtered_infos
        res_dict['tabs'] = available_tabs
        return res_dict

    def doFilteredSearch(self, tab, query):
        """
        If current tab have portal_types filter, use its portal_types.
        Else if current tab doesn't have a portal_types filter (i.e, tab "all")
        and portal_type is passed in the request and it's an available hidden index,
        use the value taken from the request. Otherwise doesn't filter for types.
        """
        tab_infos = self.tabs_mapping.get(tab, {})
        tab_types_filter = tab_infos.get('portal_types', ())
        request_portal_type = self.request.form.get('portal_type')
        types_filter = []
        if tab_types_filter:
            if request_portal_type and 'portal_type' in self.hidden_indexes:
                if request_portal_type in tab_types_filter:
                    types_filter = tab_types_filter
                else:
                    return []
            else:
                types_filter = tab_types_filter
        else:
            if request_portal_type and 'portal_type' in self.hidden_indexes:
                types_filter = request_portal_type
            else:
                return []
        query['portal_type'] = self.filter_types(types_filter)
        return self.catalog(**query)

    def getFilterInfos(self, results, filtered_results=[]):
        """
        """
        indexes_order = self.indexes_order
        indexes_mapping = self.available_indexes
        filter_dict = {}
        available_tabs = ['all']
        types_mapping = self.types_mapping
        for item in results:
            #BBB DA RIMUOVERE QUESTO IF QUANDO SI IMPLEMENTA SOLR!!!!!
            if item:
                tab_id = types_mapping.get(item.portal_type, '')
                if tab_id and tab_id not in available_tabs:
                    available_tabs.append(tab_id)
                if not filtered_results:
                    for index_id in indexes_order:
                        index_values = self.setIndexesListForItem(item, index_id)
                        if index_values:
                            if index_id not in filter_dict:
                                filter_dict[index_id] = {'title': indexes_mapping.get(index_id, index_id),
                                                         'values': {}}
                            for index_value in index_values:
                                if index_value not in filter_dict[index_id]['values']:
                                    filter_dict[index_id]['values'][index_value] = 1
                                else:
                                    filter_dict[index_id]['values'][index_value] += 1
        if filtered_results:
            for item in filtered_results:
                for index_id in indexes_order:
                    index_values = self.setIndexesListForItem(item, index_id)
                    if index_values:
                        if index_id not in filter_dict:
                            filter_dict[index_id] = {'title': indexes_mapping.get(index_id, index_id),
                                                     'values': {}}
                        for index_value in index_values:
                            if index_value not in filter_dict[index_id]['values']:
                                filter_dict[index_id]['values'][index_value] = 1
                            else:
                                filter_dict[index_id]['values'][index_value] += 1
        return filter_dict, available_tabs

    def setIndexesListForItem(self, brain, index_id):
        """
        Update results dict with indexes values of the given brain
        """
        index_value = getattr(brain, index_id, '')
        if not index_value:
            return set()
        if callable(index_value):
            return set(index_value())
        elif isinstance(index_value, tuple) or isinstance(index_value, list):
            return set(index_value)
        else:
            return set([index_value])

    def filter_query(self, query):
        """
        Make some query filtering.
        """
        request = self.request
        text = query.get('SearchableText', None)
        validation_messages = []
        if text is None:
            text = request.form.get('SearchableText', '')
        valid_keys = self.valid_keys + tuple(self.catalog.indexes())
        for k, v in request.form.items():
            if v:
                query[k] = self.setFilteredIndex(k, v, valid_keys)
        if text:
            #Check if SearchableText is too long or has too long words
            max_word_len = self.getRegistryInfos('max_word_len')
            max_words = self.getRegistryInfos('max_words')
            words = text.split()
            if len(words) > max_words:
                validation_messages.append(translate(_('search_limit_words_label',
                                                        default=u'"${word}" (and any subsequent words) was ignored because we limit queries to ${max_words} words.',
                                                        mapping={'word': words[max_words],
                                                                 'max_words': max_words}),
                                                     context=self.request))
                words = words[:max_words]
                text = " ".join(words)
            for word in words:
                if len(word) > max_word_len:
                    validation_messages.append(translate(_('search_limit_word_characters_label',
                                                        default=u'"${word}" is a too long word. Try using a shorter word.',
                                                        mapping={'word': word}),
                                                     context=self.request))
                    text.replace(word, '')
            query['SearchableText'] = quote_chars(text)
        # don't filter on created at all if we want all results
        created = query.get('created')
        if created:
            if created.get('query'):
                if created['query'][0] <= EVER:
                    del query['created']
        # respect `types_not_searched` setting
        types = query.get('portal_type', [])
        if 'query' in types:
            types = types['query']
        query['portal_type'] = self.filter_types(types)
        # respect effective/expiration date
        query['show_inactive'] = False
        # respect navigation root
        if 'path' not in query:
            query['path'] = getNavigationRoot(self.context)

        return query, validation_messages

    def getDateIndexes(self):
        """
        method that returns a list of DateIndex indexes.
        This is an hack that fix a bug in Plone timezones:
        https://dev.plone.org/ticket/13774
        """
        return [x.getId() for x in self.catalog.getIndexObjects() if isinstance(x, DateIndex)]

    def setFilteredIndex(self, key, value, valid_keys):
        """
        Add some customizations to the given query item
        """
        date_indexes = self.getDateIndexes()
        if value and ((key in valid_keys) or key.startswith('facet.')):
            if key in date_indexes:
                # Hack to fix a Plone time zone bug:
                # in the request we have a date timazone naive (GMT+0), but
                # in the index we have the right timezone (for example GMT+2).
                # this trick is copied from Products.Archetypes.Field.DateTimeField
                if isinstance(value, record):
                    query_values = value.get('query')
                    fixed_values = []
                    for v in query_values:
                        if isinstance(v, DateTime):
                            zone = v.localZone(safelocaltime(v.timeTime()))
                            parts = v.parts()[:-1] + (zone,)
                            fixed_values.append(DateTime(*parts))
                        else:
                            fixed_values.append(v)
                    value.query = fixed_values
                    return value
                elif isinstance(value, str):
                    return DateTime(value)
            else:
                if isinstance(value, list) or isinstance(value, tuple):
                    return {"query": value,
                            "operator": "and"}
                else:
                    return value
        else:
            return value

    def setQueryUrl(self, query={}, remove_indexes=[]):
        q = {}
        q.update(self.request.form)
        if query:
            q.update(query)
        if remove_indexes:
            for index in remove_indexes:
                if index in q:
                    del q[index]
        base_url = self.request.URL
        # After the AJAX call the request is changed and thus the URL part of
        # it as well. In this case we need to tweak the URL to point to have
        # correct URLs
        if '@@updated_search' in base_url:
            base_url = base_url.replace('@@updated_search', '@@search')
        return base_url + '?' + make_query(q)

    def getResultsLen(self, results_dict):
        """
        Return the updated and translated len results.
        @param: results_dict
        """
        results = results_dict.get('results', None)
        if not results:
            return 0
        results_len = results.sequence_length
        total_len = results_dict.get('tot_results_len', results_len)
        if not results_len and not total_len:
            return 0
        return translate(_("${results_len} on ${total_len}",
                          mapping={'results_len': results_len, 'total_len': total_len}),
                        context=self.request)

    def indexesChecked(self, index_name):
        """
        Return a list of selected values for a given index_id
        """
        values = self.request.form.get(index_name, None)
        if not values:
            return []
        if isinstance(values, list):
            return values
        elif getattr(values, 'query', None):
            return values.query
        else:
            return []

    ### HIDDEN INDEXES ###
    def getHiddenIndexes(self):
        """
        Return a list of hidden indexes to insert in the query
        """
        hiddenlist = self.hidden_indexes
        hidden_dict = {'index_titles': [],
                       'index_ids': [],
                     'index_to_add': []}
        if not hiddenlist:
            return hidden_dict
        for index_id in hiddenlist:
            index_value = self.context.REQUEST.form.get(index_id, '')
            if not index_value:
                continue
            register_index = False
            index_title = hiddenlist.get(index_id, index_id)
            if isinstance(index_value, record):
                register_index = self.setHiddenRecord(index_value, index_id, hidden_dict)
            elif isinstance(index_value, list):
                register_index = self.setHiddenList(index_value, index_id, hidden_dict)
            else:
                register_index = True
                hidden_dict['index_to_add'].append({'id': index_id,
                                       'value': index_value})
            if register_index:
                hidden_dict['index_titles'].append(index_title)
                hidden_dict['index_ids'].append(index_id)
        return hidden_dict

    def setHiddenList(self, index_value, index, hidden_dict):
        """
        set the hidden index if is a list
        """
        has_values = False
        for value in index_value:
            if value:
                has_values = True
                index_id = "%s:list" % (index)
                hidden_dict['index_to_add'].append({'id': index_id,
                                           'value': value})
        return has_values

    def setHiddenRecord(self, index_value, index, hidden_dict):
        """
        set the hidden index if is a record
        """
        has_values = False
        if index_value.get('query', '') in ['', ['', '']]:
            return has_values
        for query_part in index_value.keys():
            index_id = "%s.%s:record" % (index, query_part)
            query_value = index_value[query_part]
            if not query_value:
                continue
            has_values = True
            if isinstance(query_value, list):
                index_id += ":list"
                for value_item in query_value:
                    if isinstance(value_item, DateTime):
                        index_id += ":date"
                        list_value = value_item.ISO()
                        hidden_dict['index_to_add'].append({'id': index_id,
                                               'value': list_value})
                    else:
                        hidden_dict['index_to_add'].append({'id': index_id,
                                               'value': value_item})
            else:
                hidden_dict['index_to_add'].append({'id': index_id,
                                       'value': query_value})
        return has_values

    def getFolderName(self, path):
        """
        Return folder Title, if exist
        """
        folder = self.context.unrestrictedTraverse(path, None)
        if folder:
            return folder.Title()
        else:
            return path

    def filter_types(self, types):
        plone_utils = getToolByName(self.context, 'plone_utils')
        if not isinstance(types, list) and not isinstance(types, tuple):
            types = [types]
        return plone_utils.getUserFriendlyTypes(types)

    def sort_options(self):
        """ Sorting options for search results view. """
        return (
            SortOption(self.request, _(u'relevance'), ''),
            SortOption(self.request, _(u'date (newest first)'),
                       'Date', reverse=True),
            SortOption(self.request, _(u'alphabetically'), 'sortable_title'),
        )
