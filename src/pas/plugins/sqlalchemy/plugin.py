# -*- coding: utf-8 -*-
from AccessControl import ClassSecurityInfo
from AccessControl.SecurityManagement import getSecurityManager
from Acquisition import aq_get
from DateTime import DateTime
# from Globals import InitializeClass
from AccessControl.class_init import InitializeClass
from OFS.Cache import Cacheable
from pas.plugins.sqlalchemy import model
from Products.CMFCore.utils import getToolByName
from Products.PageTemplates.PageTemplateFile import PageTemplateFile
from Products.PlonePAS.interfaces.capabilities import IAssignRoleCapability
from Products.PlonePAS.interfaces.capabilities import IDeleteCapability
from Products.PlonePAS.interfaces.capabilities import IGroupCapability
from Products.PlonePAS.interfaces.capabilities import IPasswordSetCapability
from Products.PlonePAS.interfaces.group import IGroupIntrospection
from Products.PlonePAS.interfaces.group import IGroupManagement
from Products.PlonePAS.interfaces.plugins import IMutablePropertiesPlugin
from Products.PlonePAS.interfaces.plugins import IUserManagement
from Products.PlonePAS.interfaces.propertysheets import IMutablePropertySheet
from Products.PlonePAS.plugins.group import PloneGroup
from Products.PluggableAuthService.events import PropertiesUpdated
from Products.PluggableAuthService.interfaces import plugins as pasplugins
from Products.PluggableAuthService.permissions import ManageGroups
from Products.PluggableAuthService.permissions import ManageUsers
from Products.PluggableAuthService.permissions import SetOwnPassword
from Products.PluggableAuthService.plugins.BasePlugin import BasePlugin
from Products.PluggableAuthService.UserPropertySheet import UserPropertySheet
from Products.PluggableAuthService.utils import createViewName
from sqlalchemy import sql
from z3c.saconfig import named_scoped_session
from zope.component.interfaces import ComponentLookupError
from zope.dottedname.resolve import resolve
from zope.event import notify
from zope.interface import implementer
import datetime
import logging
import sqlalchemy as rdb
import time
import traceback

Session = named_scoped_session("pas.plugins.sqlalchemy")

logger = logging.getLogger("pas.plugins.sqlalchemy")

manage_addSqlalchemyPluginForm = PageTemplateFile(
    "templates/addPlugin",
    globals(),
    __name__="manage_addPlugin"
)


def addSqlalchemyPlugin(self, id, title="", user_model=None,
                        principal_model=None, group_model=None, REQUEST=None):
    """Add an SQLAlchemy plugin to a PAS."""
    p = Plugin(id, title)
    p.user_model = user_model
    p.principal_model = principal_model
    p.group_model = group_model
    self._setObject(p.getId(), p)

    if REQUEST is not None:
        REQUEST.response.redirect(
            "%s/manage_workspace?manage_tabs_message=SQLAlchemy+plugin+"
            "added." % self.absolute_url()
        )


def safeencode(v):
    if isinstance(v, str):
        return v.encode('utf-8')
    return v


def safedecode(v):
    if isinstance(v, bytes):
        return v.decode('utf-8')
    return v


_V_ERROR_MARKER = '_v_pas.plugins.sqlalchemy_errored'


def graceful_recovery(default=None, log_args=True):
    def decorator(func):
        def wrapper(*args, **kwargs):
            try:
                value = func(*args, **kwargs)
            except ComponentLookupError as e:
                # XXX this may mask different ComponentLookupErrors as well!
                try:
                    exc_str = str(e)
                except Exception as e:
                    exc_str = "<%s at 0x%x>" % (e.__class__.__name__, id(e))
                # only log once every 3 seconds
                if getattr(args[0], _V_ERROR_MARKER, 0) < time.time() - 3:
                    setattr(args[0], _V_ERROR_MARKER, time.time())
                    logger.critical(
                        "Apparently we haven't yet configured a z3c.saconfig "
                        "connection. This may mask other "
                        "ComponentLookupErrors as well!\n{0:s}".format(exc_str)
                    )

                return default
            except rdb.exc.SQLAlchemyError as e:
                if log_args is False:
                    args = ()
                    kwargs = {}

                formatted_tb = traceback.format_exc()

                try:
                    exc_str = str(e)
                except Exception as e:
                    exc_str = "<%s at 0x%x>" % (e.__class__.__name__, id(e))

                logger.critical(
                    "caught SQL-exception: "
                    "%s (in method ``%s``; arguments were %s)\n\n%s" % (
                        exc_str,
                        func.__name__, ", ".join(
                            [repr(arg) for arg in args] +
                            ["%s=%s" % (name, repr(value))
                             for (name, value) in kwargs.items()]
                        ), formatted_tb))
                return default
            return value
        return wrapper
    return decorator


@implementer(IMutablePropertySheet)
class MutablePropertySheet(UserPropertySheet):
    """Mutable property sheet that persists changes back via plugin."""

    def __init__(self, plugin, **kwargs):
        UserPropertySheet.__init__(self, plugin.id, **kwargs)
        self._plugin = plugin

    def __getitem__(self, key):
        return self.getProperty(key)

    def __iter__(self):
        return self.propertyIds()

    def get(self, key, default):
        return self.getProperty(key, default)

    def canWriteProperty(self, object, id):
        return True

    def setProperty(self, object, id, value):
        self._properties[id] = value
        self._plugin.doSetProperty(object, id, value)

    def setProperties(self, object, mapping):
        self._properties.update(mapping)
        self._plugin.setPropertiesForUser(object, self)


@implementer(
    pasplugins.IAuthenticationPlugin,
    pasplugins.IUserEnumerationPlugin,
    pasplugins.IUserAdderPlugin,
    IUserManagement,
    IDeleteCapability,
    IPasswordSetCapability,
    pasplugins.IRolesPlugin,
    pasplugins.IRoleAssignerPlugin,
    IAssignRoleCapability,
    IGroupCapability,
    pasplugins.IPropertiesPlugin,
    IMutablePropertiesPlugin,
    pasplugins.IGroupsPlugin,
    pasplugins.IGroupEnumerationPlugin,
    IGroupIntrospection,
    IGroupManagement
)
class Plugin(BasePlugin, Cacheable):
    meta_type = 'SQLAlchemy user/group/prop manager'
    security = ClassSecurityInfo()
    manage_options = BasePlugin.manage_options + Cacheable.manage_options

    _properties = BasePlugin._properties + (
        {'id': 'user_model',
         'label': 'SQLAlchemy User model (dotted path)',
         'type': 'string',
         'mode': 'w',
         },
        {'id': 'principal_model',
         'label': 'SQLAlchemy Principal model (dotted path)',
         'type': 'string',
         'mode': 'w',
         },
        {'id': 'group_model',
         'label': 'SQLAlchemy Group model (dotted path)',
         'type': 'string',
         'mode': 'w',
         }
    )

    user_model = "pas.plugins.sqlalchemy.model.User"
    principal_model = "pas.plugins.sqlalchemy.model.Principal"
    group_model = "pas.plugins.sqlalchemy.model.Group"

    def __init__(self, id, title=None, user_model=None,
                 principal_model=None, group_model=None):
        self.id = self.id = id
        self.title = title
        if user_model:
            self.user_model = user_model
        if principal_model:
            self.principal_model = principal_model
        if group_model:
            self.group_model = group_model

    @security.private
    def invalidateCacheForChangedUser(self, user_id):
        pass

    @property
    def principal_class(self):
        cls = getattr(self, "_v_principal_class", None)
        if cls is None:
            try:
                cls = self._v_principal_class = resolve(self.principal_model)
            except ImportError as e:
                logger.error("Unable to import user model: %s", e)
                cls = self._v_principal_class = model.Principal
        return cls

    @property
    def user_class(self):
        cls = getattr(self, "_v_user_class", None)
        if cls is None:
            try:
                cls = self._v_user_class = resolve(self.user_model)
            except ImportError as e:
                logger.error("Unable to import user model: %s", e)
                cls = self._v_user_class = model.User
        return cls

    @property
    def group_class(self):
        cls = getattr(self, "_v_group_class", None)
        if cls is None:
            try:
                cls = self._v_group_class = resolve(self.group_model)
            except ImportError as e:
                logger.error("Unable to import group model: %s", e)
                cls = self._v_group_class = model.Group
        return cls

    #
    # IUserManagement implementation
    #

    @security.private
    @graceful_recovery()
    def doChangeUser(self, principal_id, password, **kw):
        # userSetPassword in PlonePAS expects a RuntimeError when a
        # plugin doesn't hold the user.
        session = Session()
        query = session.query(self.user_class).filter_by(zope_id=principal_id)
        user = query.first()
        if user is None:
            raise RuntimeError(
                "User does not exist: zope_id=%s" % principal_id
            )
        user.set_password(password)

    @security.private
    @graceful_recovery()
    def doDeleteUser(self, login):
        session = Session()
        user = session.query(self.user_class).filter_by(login=login).first()
        if user is None:
            return False
        session.delete(user)
        return True

    #
    # IPasswordSetCapability implementation
    #
    @security.public
    @graceful_recovery(False)
    def allowPasswordSet(self, userid):
        session = Session()
        user = session.query(self.user_class).filter_by(zope_id=userid).first()
        return user is not None

    #
    # IAuthenticationPlugin implementation
    #

    @security.private
    @graceful_recovery(log_args=False)
    def authenticateCredentials(self, credentials):
        login = credentials.get('login')
        password = credentials.get('password')

        if not login or not password:
            return None

        session = Session()
        user = session.query(self.user_class).filter_by(login=login).first()

        if user is not None and user.check_password(password):
            return (user.zope_id, user.login)

    #
    # IUserEnumerationPlugin implementation
    #
    def _enumerate(self, cls, exact_match, sort_by, max_results, criteria):
        """Helper method for enumerateUsers and enumerateGroups.
        """

        if exact_match and not ("login" in criteria or "id" in criteria):
            return ()

        view_name = createViewName(
            'enumerate%s' % cls.__name__,
            criteria.get("id", None) or criteria.get("login", None)
        )
        cachekey = {
            '_exact_match': exact_match,
            '_sort_by': sort_by,
            '_max_results': max_results,
        }
        cachekey.update(criteria)

        cached_info = self.ZCacheable_get(
            view_name=view_name, keywords=cachekey)
        if cached_info is not None:
            return cached_info

        def clause(column, value):
            if exact_match or not isinstance(value, basestring):
                return (column == value)
            elif isinstance(value, str):
                return column.ilike("%%%s%%" % value)
            elif isinstance(value, unicode):
                return column.ilike(u"%%%s%%" % value)
            return (column == v)

        session = Session()
        query = session.query(cls)

        propmap = dict(cls._properties)
        for (term, value) in criteria.items():
            column = getattr(cls, propmap[term])
            if not (isinstance(value, list) or isinstance(value, tuple)):
                query = query.filter(clause(column, value))
            else:
                parts = [clause(column, v) for v in value]
                query = query.filter(sql.or_(*parts))

        if sort_by is not None and sort_by in propmap:
            query = query.order_by(getattr(cls[sort_by]))
        if max_results is not None:
            query = query.limit(max_results)

        all = {}
        for user in query:
            user_id = user.zope_id
            data = dict(id=safeencode(user_id),
                        pluginid=self.getId())
            if "login" in propmap:
                data["login"] = user.login
            all[user_id] = data.setdefault(user_id, data)

        values = tuple(all.values())
        # Cache data upon success
        self.ZCacheable_set(values, view_name=view_name, keywords=cachekey)

        return values

    @security.private
    @graceful_recovery(())
    def enumerateUsers(self, id=None, login=None, exact_match=False,
                       sort_by=None, max_results=None, **kw):
        """See IUserEnumerationPlugin."""

        if exact_match and not (login or id):
            return ()

        if id:
            kw["id"] = id
        if login:
            kw["login"] = login

        return self._enumerate(
            self.user_class, exact_match, sort_by, max_results, kw
        )


    @security.private
    @graceful_recovery()
    def updateUser(self, user_id, login):  # raises keyerror
        session = Session()
        query = session.query(self.user_class).filter_by(zope_id=user_id)
        user = query.first()
        if user is None:
            raise KeyError(user_id)
        else:
            user.set_login(login)
            return True


    #
    # IUserAdderPlugin implementation
    #
    @security.private
    def doAddUser(self, login, password):
        try:
            self.addUser(login, login, password)
        except KeyError:
            return False
        return True

    @security.private
    @graceful_recovery(log_args=False)
    def addUser(self, user_id, login_name, password):
        session = Session()
        new_user = self.user_class(zope_id=user_id, login=login_name)
        new_user.set_password(password)
        session.add(new_user)

    @security.private
    @graceful_recovery()
    def removeUser(self, user_id):  # raises keyerror
        session = Session()
        query = session.query(self.user_class).filter_by(zope_id=user_id)
        user = query.first()
        if user is None:
            raise KeyError(user_id)

        session.delete(user)

    #
    # Allow users to change their own login name and password.
    #
    @security.protected(SetOwnPassword)
    def getOwnUserInfo(self):
        """Return current user's info."""

        user_id = getSecurityManager().getUser().getId()
        return self.getUserInfo(user_id)

    def allowRoleAssign(self, principal_id, role_id):
        return True

    @security.private
    def doRemoveRoleFromPrincipal(self, principal_id, role):
        return self.removeRoleFromPrincipal(role, principal_id)

    @security.protected(ManageUsers)
    def removeRoleFromPrincipal(self, role_id, principal_id):
        """ Remove a role from a principal (user or group).

        o Return a boolean indicating whether the role was already present.

        o Raise KeyError if 'role_id' is unknown.

        o Ignore requests to remove a role not already assigned to the
          principal.
        """
        roles = self.getRolesForPrincipal(principal_id, ignore_groups=True)
        if role_id in roles:
            self.doRemoveRolesFromPrincipal([role_id], principal_id)

            view_name = createViewName(
                'getRolesForPrincipal-IgnDirFalse-IgnGrpFalse', principal_id
            )
            self.ZCacheable_invalidate(view_name)
            return True

        return False

    @security.private
    def doRemoveRolesFromPrincipal(self, roles, principal_id):
        principal = self._get_principal_by_id(principal_id)
        for role in roles:
            principal.roles.remove(role)

    @security.protected(ManageUsers)
    def assignRolesToPrincipal(self, roles, principal_id):
        """Assign a specific set of roles, and only those roles, to a
        principal.

        o no return value
        o insert and delete roles on the SQL Backend based on the roles
          parameter
        """
        ignored_roles = ('Authenticated', 'Anonymous', 'Owner')
        roles = [role_id for role_id in roles if role_id not in ignored_roles]

        # remove actual roles that are not in the roles parameter
        actual_roles = self.getRolesForPrincipal(
            principal_id, ignore_groups=True
        )

        self.doRemoveRolesFromPrincipal(
            [role for role in actual_roles if role not in roles], principal_id)

        # insert new roles
        for role in roles:
            if role not in ignored_roles:
                self.doAssignRoleToPrincipal(
                    principal_id, role, invalidate_cache=False
                )

        view_name = createViewName(
            'getRolesForPrincipal-IgnDirFalse-IgnGrpFalse', principal_id
        )

        self.ZCacheable_invalidate(view_name)

    @security.private
    def doAssignRoleToPrincipal(
            self, principal_id, role, invalidate_cache=True):
        """ Create a principal/role association in a Role Manager

        o Return a Boolean indicating whether the role was assigned or not
        """

        principal = self._get_principal_by_id(principal_id)
        if principal is None or role in principal.roles:
            return False

        principal.roles.add(role)

        if invalidate_cache:
            view_name = createViewName(
                'getRolesForPrincipal-IgnDirFalse-IgnGrpFalse', principal_id
            )
            self.ZCacheable_invalidate(view_name)

        return True

    @security.private
    @graceful_recovery(())
    def getRolesForPrincipal(
            self, principal, request=None, ignore_groups=False):
        """ principal -> ( role_1, ... role_N )

        o Return a sequence of role names which the principal has.

        o May assign roles based on values in the REQUEST object, if present.
        """

        roles = set([])
        principal_ids = set([])

        if isinstance(principal, basestring):
            # This is an extension to the official PAS plugin for internal use.
            principal_id = principal
        else:
            principal_id = principal.getId()

        # Adapted from
        # Products.PlonePAS.plugins.roles.getRolesForPrincipal. Don't
        # like it!

        request = aq_get(self, 'REQUEST', None)
        # Some services need to determine the roles obtained from groups
        # while excluding the directly assigned roles.  In this case
        # '__ignore_direct_roles__' = True should be pushed in the request.
        __ignore_direct_roles__ = request and request.get(
            '__ignore_direct_roles__', False
        )

        # Some services may need the real roles of an user but **not**
        # the ones he got through his groups. In this case, the
        # '__ignore_group_roles__'= True should be previously pushed
        # in the request.
        __ignore_group_roles__ = (
            request and request.get('__ignore_group_roles__', False)
        ) or ignore_groups

        method_name = 'getRolesForPrincipal-IgnDir%s-IgnGrp%s' % (
            str(__ignore_direct_roles__),
            str(__ignore_group_roles__)
        )

        view_name = createViewName(method_name, principal_id)
        cached_info = self.ZCacheable_get(view_name)
        if cached_info is not None:
            return cached_info

        sql_principal = self._get_principal_by_id(principal_id)
        if sql_principal is None:
            return ()

        if not __ignore_direct_roles__:
            principal_ids.add(principal_id)
        if not __ignore_group_roles__:
            for groups in self._get_groups_for_principal_from_pas(principal):
                principal_ids.update(groups)

        for pid in principal_ids:
            sql_principal = self._get_principal_by_id(pid)
            if sql_principal:
                roles.update(sql_principal.roles)

        roles = tuple(roles)
        self.ZCacheable_set(roles, view_name=view_name)
        return roles

    #
    # IMutablePropertiesPlugin implementation
    #

    def _getSchema(self, isgroup=None):
        # this could probably stand to be cached
        datatool = isgroup and "portal_groupdata" or "portal_memberdata"
        schema = getattr(self, '_schema', None)
        if not schema:
            # if no schema is provided, use portal_memberdata properties
            schema = ()
            mdtool = getToolByName(self, datatool, None)
            # Don't fail badly if tool is not available.
            if mdtool is not None:
                mdschema = mdtool.propertyMap()
                schema = [(elt['id'], elt['type']) for elt in mdschema]
        return schema

    @security.private
    @graceful_recovery()
    def getPropertiesForUser(self, user, request=None):
        """Get property values for a user or group.
        Returns a dictionary of values or a PropertySheet.
        """
        isGroup = getattr(user, 'isGroup', lambda: None)()

        view_name = createViewName('getPropertiesForUser', user.getId())
        cached_info = self.ZCacheable_get(view_name=view_name)
        schema = self._getSchema(isGroup) or None
        if cached_info is not None:
            return MutablePropertySheet(
                self, schema=schema, **cached_info
            )

        session = Session()
        column_name = dict(self.principal_class._properties).get(
            'zope_id',  # map zope_id
            'zope_id'   # if not mapped fall back to 'zope_id'
        )
        filter_data = {column_name: user.getId()}

        # Be sure that, if it's a real user, it belongs to this plugin
        if not isGroup:
            is_external_user = session.query(self.user_class).filter_by(
                **filter_data
            ).count() == 0
            if is_external_user:
                return None

        query = session.query(self.principal_class).filter_by(**filter_data)
        principal = query.first()
        if principal is None:
            # XXX: Should we cache a negative result?
            # return MutablePropertySheet(self, schema=schema)
            # return none if user not exists
            return None

        data = {}
        for (zope_attr, sql_attr) in principal._properties:
            value = getattr(principal, sql_attr)

            if isinstance(value, datetime.datetime) or \
                    isinstance(value, datetime.date):
                value = DateTime(value.isoformat())
            data[zope_attr] = value
        if data:
            self.ZCacheable_set(data, view_name=view_name)
            data.pop('id', None)
            return MutablePropertySheet(self, schema=schema, **data)

    @security.private
    def doSetProperty(self, principal, name, value):
        username = principal.getId()
        principal = self._get_principal_by_id(username)
        if not principal:
            return

        propmap = dict([reversed(r) for r in principal._properties])
        sql_attr = propmap.get(name, None)
        if sql_attr is None:
            return

        if isinstance(value, DateTime):
            value = value.utcdatetime()

        # if value is a string, make sure it does not exceed the limit
        # (truncate if necessary--this is better than breaking the
        # application)
        if isinstance(value, basestring):
            value = safedecode(value)
            cspec = getattr(principal.__mapper__.columns, sql_attr).type
            if isinstance(cspec, rdb.String):
                value = value[:cspec.length]
        setattr(principal, sql_attr, value)

    @security.private
    @graceful_recovery()
    def setPropertiesForUser(self, user, propertysheet):
        username = user.getId()
        principal = self._get_principal_by_id(username)
        properties = propertysheet.propertyItems()

        for name, value in properties:
            self.doSetProperty(principal, name, value)

        try:
            event = PropertiesUpdated(user, properties)
        except TypeError:
            # BBB: See Launchpad #795086
            event = object.__new__(PropertiesUpdated)
            event.object = user
            event.principal = user
            event.properties = properties

        # XXX: This event is not fired by PAS!
        notify(event)

        view_name = createViewName('getPropertiesForUser', user)
        self.ZCacheable_invalidate(view_name=view_name)

    #
    # IGroupsPlugin implementation
    #

    @security.private
    @graceful_recovery(())
    def getGroupsForPrincipal(self, principal, request=None):
        """ principal -> ( group_1, ... group_N )

        o Return a sequence of group names to which the principal
          (either a user or another group) belongs.

        o May assign groups based on values in the REQUEST object, if present
        """

        if isinstance(principal, basestring):
            principal_id = principal
        else:
            principal_id = principal.getId()

        session = Session()
        principal = session.query(self.principal_class)\
            .filter_by(zope_id=principal_id).first()
        if principal is None:
            return ()

        return [group.zope_id for group in principal.groups]

    #
    # IGroupsEnumeration implementation
    #

    @security.private
    @graceful_recovery(())
    def enumerateGroups(self, id=None,
                        exact_match=False,
                        sort_by=None,
                        max_results=None,
                        **kw
                        ):
        """ -> ( group_info_1, ... group_info_N )

        o Return mappings for groups matching the given criteria.

        o 'id' in combination with 'exact_match' true, will
          return at most one mapping per supplied ID ('id' and 'login'
          may be sequences).

        o If 'exact_match' is False, then 'id' may be treated by
          the plugin as "contains" searches (more complicated searches
          may be supported by some plugins using other keyword arguments).

        o If 'sort_by' is passed, the results will be sorted accordingly.
          known valid values are 'id' (some plugins may support others).

        o If 'max_results' is specified, it must be a positive integer,
          limiting the number of returned mappings.  If unspecified, the
          plugin should return mappings for all groups satisfying the
          criteria.

        o Minimal keys in the returned mappings:

          'id' -- (required) the group ID

          'pluginid' -- (required) the plugin ID (as returned by getId())

          'properties_url' -- (optional) the URL to a page for updating the
                              group's properties.

          'members_url' -- (optional) the URL to a page for updating the
                           principals who belong to the group.

        o Plugin *must* ignore unknown criteria.

        o Plugin may raise ValueError for invalid critera.

        o Insufficiently-specified criteria may have catastrophic
          scaling issues for some implementations.
        """

        if id:
            kw["id"] = id

        return self._enumerate(
            self.group_class, exact_match, sort_by, max_results, kw
        )

    ####################
    # IGroupManagement
    ####################

    @security.private
    @graceful_recovery(False)
    def addGroup(self, id, **kw):
        """
        Create a group with the supplied id, roles, and groups.
        return True if the operation suceeded
        """

        if self.enumerateGroups(id):
            raise KeyError('Duplicate group ID: %s' % id)

        session = Session()
        group = self.group_class(zope_id=id)
        session.add(group)

        return True

    @security.protected(ManageGroups)
    @graceful_recovery(False)
    def addPrincipalToGroup(self, principal_id, group_id):
        """
        Add a given principal to the group.
        return True on success
        """

        session = Session()
        query = session.query(self.group_class).filter_by(zope_id=group_id)
        group = query.first()
        if group is None:
            return False

        principal = session.query(self.principal_class)\
            .filter_by(zope_id=principal_id).first()

        if principal is None:
            # Last chance:
            # maybe we are trying to add a users not handled by this plugin?
            acl_users = getToolByName(self, 'acl_users')
            principal = acl_users.getUser(principal_id)
            if not principal:
                return False
            principal = self._add_principal(zope_id=principal.getId())

        group.members.append(principal)
        return True

    @security.private
    @graceful_recovery(False)
    def removeGroup(self, group_id):
        """
        Remove the given group
        return True on success
        """

        session = Session()
        query = session.query(self.group_class).filter_by(zope_id=group_id)
        group = query.first()
        if group is not None:
            session.delete(group)
            return True

        return False

    @security.protected(ManageGroups)
    @graceful_recovery(False)
    def removePrincipalFromGroup(self, principal_id, group_id):
        """
        Remove the given principal from the group; return True on success.
        """

        session = Session()

        group = session.query(self.group_class)\
            .filter_by(zope_id=group_id).first()
        user = session.query(self.principal_class)\
            .filter_by(zope_id=principal_id).first()

        if group is None or user is None:
            return False

        group.members.remove(user)

        # If user doesn't belongs to this plugin, garbage can be left behind
        # in that case we can have orphan items in the principals table
        full_user = session.query(self.user_class).filter_by(id=user.id)\
            .first()
        relations_left = session.query(model.group_member_table).filter(
            model.group_member_table.c.principal_id == user.id
            ).count()
        if not full_user and relations_left == 0:
            session.query(self.principal_class).filter_by(id=user.id)\
                .delete()
        return True

    @security.private
    def updateGroup(self, group_id, title=None, description=None):
        session = Session()
        principal = session.query(self.principal_class).\
            filter_by(zope_id=group_id).first()
        if title:
            self.doSetProperty(principal, 'title', title)
        if description:
            self.doSetProperty(principal, 'description', description)

        view_name = createViewName('getPropertiesForUser', group_id)
        self.ZCacheable_invalidate(view_name=view_name)

    #
    #   IDeleteCapability implementation
    #

    @security.public
    @graceful_recovery(False)
    def allowDeletePrincipal(self, principal_id):
        """True if this plugin can delete a certain principal."""
        return self._get_principal_by_id(principal_id) is not None

    #
    #   IGroupCapability implementation
    #

    @graceful_recovery(False)
    def allowGroupAdd(self, user_id, group_id):
        """True if this plugin will allow adding a certain user to a
        certain group."""

        session = Session()
        query = session.query(self.group_class).filter_by(zope_id=group_id)
        group = query.first()
        if group is None:
            return False

        if user_id in [member.zope_id for member in group.members]:
            return False

        return True

    @graceful_recovery(False)
    def allowGroupRemove(self, user_id, group_id):
        """True if this plugin will allow removing a certain user from
        a certain group."""

        present = self.enumerateGroups(id=group_id)

        # if we don't have a group, we can't do anything
        if not present:
            return False

        groups = self.getGroupsForPrincipal(user_id)
        if group_id in groups:
            return True

        return False

    ###########################
    # IGroupIntrospection
    ###########################

    @graceful_recovery(None)
    def getGroupById(self, group_id):
        """
        Returns the portal_groupdata-ish object for a group
        corresponding to this id.
        """

        if group_id and self.enumerateGroups(group_id):
            group = PloneGroup(group_id, None)

            for name, data in self._get_properties_for_user_from_pas(group):
                group.addPropertysheet(name, data)

            for roles in self._get_roles_for_principal_from_pas(group):
                group._addRoles(roles)

            for groups in self._get_groups_for_principal_from_pas(group):
                group._addGroups(groups)

            group._addRoles(['Authenticated'])

            return group.__of__(self)
        else:
            return None

    #################################
    # these interface methods are suspect for scalability.
    #################################

    @graceful_recovery(())
    def getGroups(self):
        """
        Returns an iteration of the available groups
        """

        session = Session()
        groups = session.query(self.group_class).all()
        return [PloneGroup(g.zope_id).__of__(self) for g in groups]

    @graceful_recovery(())
    def getGroupIds(self):
        """
        Returns a list of the available groups
        """

        session = Session()
        query = session.query(self.group_class.zope_id)
        return [row[0] for row in query.all()]

    @graceful_recovery(())
    def getGroupMembers(self, group_id):
        """
        Return the members of the given group
        """

        session = Session()
        query = session.query(self.group_class).filter_by(zope_id=group_id)
        group = query.first()
        if group is None:
            return []
        return [member.zope_id for member in group.members]

    # PlonePAS expects plugins implementing IRoleAssignerPlugin to
    # implement addRole. (In addRole in pas).  The method is not
    # specified in the IRoleAssignerPlugin interface, so this is bad.
    @security.protected(ManageUsers)
    def addRole(self, role_id, title='', description=''):
        # We do not manage roles.
        raise AttributeError

    def _add_principal(self, zope_id, type="user"):
        session = Session()
        new_principal = self.principal_class(type=type, zope_id=zope_id)
        session.add(new_principal)
        return new_principal

    def _get_groups_for_principal_from_pas(self, principal):
        plugins = self._getPAS()._getOb('plugins')

        for name, plugin in plugins.listPlugins(pasplugins.IGroupsPlugin):
            groups = plugin.getGroupsForPrincipal(principal)
            if groups:
                yield groups

    def _get_properties_for_user_from_pas(self, principal):
        plugins = self._getPAS()._getOb('plugins')
        propfinders = plugins.listPlugins(pasplugins.IPropertiesPlugin)
        for propfinder_id, propfinder in propfinders:
            data = propfinder.getPropertiesForUser(principal, request=None)
            if data:
                yield propfinder_id, data

    def _get_roles_for_principal_from_pas(self, principal):
        plugins = self._getPAS()._getOb('plugins')
        rolemakers = plugins.listPlugins(pasplugins.IRolesPlugin)

        for rolemaker_id, rolemaker in rolemakers:
            roles = rolemaker.getRolesForPrincipal(principal, request=None)
            if roles:
                yield roles

    def _get_principal_by_id(self, principal_id):
        session = Session()
        query = session.query(self.principal_class).filter_by(
            zope_id=principal_id
        )
        return query.first()


InitializeClass(Plugin)
