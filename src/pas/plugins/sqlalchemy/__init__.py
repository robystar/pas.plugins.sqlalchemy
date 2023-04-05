# -*- coding: utf-8 -*-
from Products.PluggableAuthService.PluggableAuthService import MultiPlugins
from Products.PluggableAuthService.PluggableAuthService import \
    registerMultiPlugin
# import plugin
from pas.plugins.sqlalchemy.plugin import (
    Plugin,
    manage_addSqlalchemyPluginForm,
    addSqlalchemyPlugin
    )
from AccessControl.Permissions import add_user_folders
import os

plugins = set()


def initialize(context):
    if Plugin.meta_type not in MultiPlugins:
        registerMultiPlugin(Plugin.meta_type)
        context.registerClass(
            Plugin,
            permission=add_user_folders,
            icon=os.path.join(os.path.dirname(os.path.realpath(__file__)),
                              'www', 'sql.png'),
            constructors=(manage_addSqlalchemyPluginForm,
                          addSqlalchemyPlugin),
            visibility=None
        )
