# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2016, 2017 CERN.
#
# Invenio is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Invenio is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place, Suite 330, Boston,
# MA 02111-1307, USA.
#
# In applying this license, CERN does not
# waive the privileges and immunities granted to it by virtue of its status
# as an Intergovernmental Organization or submit itself to any jurisdiction.

"""Invenio modules that adds accounts REST API."""

from __future__ import absolute_import, print_function

from functools import wraps

from flask import Blueprint, abort, current_app, request, url_for
from flask_security.changeable import encrypt_password
from flask_security.signals import password_changed
from flask_security.utils import verify_password
from invenio_accounts.models import Role, User, userrole
from invenio_db import db
from invenio_oauth2server import require_api_auth
from invenio_rest import ContentNegotiatedMethodView
from jsonpatch import apply_patch
from sqlalchemy import String, cast, func, orm
from werkzeug.local import LocalProxy

from invenio_accounts_rest.errors import MaxResultWindowRESTError
from invenio_accounts_rest.loaders import default_json_loader_with_profile, \
    default_json_loader_without_profile, \
    default_json_patch_loader_with_profile, \
    default_json_patch_loader_without_profile
from invenio_accounts_rest.proxies import current_accounts_rest
from invenio_accounts_rest.serializers import role_serializer, \
    roles_list_serializer, status_code_serializer, user_serializer, \
    user_with_profile_serializer, users_list_serializer, \
    users_with_profile_list_serializer
from .errors import MissingOldPasswordError

blueprint = Blueprint(
    'invenio_accounts_rest',
    __name__,
)

_datastore = LocalProxy(lambda: current_app.extensions['security'].datastore)


def verify_list_permission(permission_factory):
    """Check that the current user has permissions on roles or users list.

    In case the permission check fails, an Flask abort is launched.
    If the user was previously logged-in, a HTTP error 403 is returned.
    Otherwise, is returned a HTTP error 401.

    :param permission_factory: permission factory used to check permissions.
    """
    # Note, cannot be done in one line due overloading of boolean
    # operations permission object.
    with current_app.app_context():
        if not permission_factory().can():
            from flask_login import current_user
            if not current_user.is_authenticated:
                abort(401)
            abort(403)


def need_list_permission(factory_name):
    """Decorator checking that the user has permissions on roles or users list.

    :param factory_name: name of the factory to retrieve.
    """
    def need_list_permission_builder(f):
        @wraps(f)
        def need_list_permission_decorator(self, *args, **kwargs):
            permission_factory = getattr(current_accounts_rest,
                                         factory_name)()
            request._methodview = self
            if permission_factory:
                verify_list_permission(permission_factory)

            return f(self, *args, **kwargs)
        return need_list_permission_decorator
    return need_list_permission_builder


def paginated_query_links(endpoint, total, page, size, max_result_window,
                          **kwargs):
    """Create links of a paginated search query."""
    result_links = {}
    if page > 1:
        result_links['prev'] = url_for(endpoint, size=size,
                                       page=page - 1, _external=True, **kwargs)

    if size * page < total and \
            size * page < max_result_window:
        result_links['next'] = url_for(endpoint, size=page,
                                       page=page + 1, _external=True, **kwargs)

    return result_links


class RolesListResource(ContentNegotiatedMethodView):
    """Roles list resource."""

    view_name = 'list_roles'

    def __init__(self, max_result_window=None, **kwargs):
        """Constructor."""
        super(RolesListResource, self).__init__(
            method_serializers={
                'POST': {
                    'application/json': role_serializer,
                },
                'GET': {
                    'application/json': roles_list_serializer,
                }
            },
            default_media_type='application/json',
            **kwargs
        )
        self.max_result_window = max_result_window or 10000

    @need_list_permission('read_roles_list_permission_factory')
    def get(self):
        """Get a list of all roles."""
        page = request.values.get('page', 1, type=int)
        size = request.values.get('size', 10, type=int)
        if page * size >= self.max_result_window:
            raise MaxResultWindowRESTError()

        query_string = request.args.get('q')
        role_query = Role.query.order_by(Role.name)
        total_query = db.session.query(func.count(Role.id))
        if query_string is not None:
            query_filter = Role.name.like('%{}%'.format(query_string))
            role_query = role_query.filter(query_filter)
            total_query = total_query.filter(query_filter)
        roles = role_query.slice((page - 1) * size, page * size).all()
        total = total_query.scalar()

        result = self.make_response(
            roles=roles,
            total=total,
            links=paginated_query_links('invenio_accounts_rest.list_roles',
                                        total, page, size,
                                        self.max_result_window),
            code=200,
        )

        return result

    @need_list_permission('create_role_permission_factory')
    def post(self):
        """Create a new role."""
        posted_role = _datastore.create_role(**request.get_json())
        db.session.commit()
        return self.make_response(posted_role, 201)


def pass_role(f):
    """Decorator to retrieve a role."""
    @wraps(f)
    def inner(self, role_id, *args, **kwargs):
        role = Role.query.filter(Role.id == role_id).one_or_none()
        if role is None:
            abort(404)
        return f(self, role=role, *args, **kwargs)
    return inner


def verify_role_permission(permission_factory, role):
    """Check that the current user has the required permissions on a role.

    In case the permission check fails, an Flask abort is launched.
    If the user was previously logged-in, a HTTP error 403 is returned.
    Otherwise, is returned a HTTP error 401.

    :param permission_factory: permission factory used to check permissions.
    :param role: role with limited access.
    """
    # Note, cannot be done in one line due overloading of boolean
    # operations permission object.
    with current_app.app_context():
        if not permission_factory(role=role).can():
            from flask_login import current_user
            if not current_user.is_authenticated:
                abort(401)
            abort(403)


def need_role_permission(factory_name):
    """Decorator checking that the user has the permissions on a role.

    :param factory_name: name of the factory to retrieve.
    """
    def need_role_permission_builder(f):
        @wraps(f)
        def need_role_permission_decorator(self, role=None, *args, **kwargs):
            permission_factory = getattr(current_accounts_rest,
                                         factory_name)()
            request._methodview = self
            if permission_factory:
                verify_role_permission(permission_factory, role)

            return f(self, role=role, *args, **kwargs)
        return need_role_permission_decorator
    return need_role_permission_builder


class RoleResource(ContentNegotiatedMethodView):
    """Role resource."""

    view_name = 'role'

    def __init__(self, **kwargs):
        """Constructor."""
        super(RoleResource, self).__init__(
            method_serializers={
                'GET': {
                    'application/json': role_serializer,
                },
                'DELETE': {
                    'application/json': role_serializer,
                },
                'PATCH': {
                    'application/json-patch+json': role_serializer,
                }
            },
            serializers={
                'application/json': role_serializer
            },
            default_media_type='application/json',
            **kwargs
        )

    @pass_role
    @need_role_permission('read_role_permission_factory')
    def get(self, role):
        """Get a role with a given id."""
        return self.make_response(role, 200)

    @pass_role
    @need_role_permission('update_role_permission_factory')
    def patch(self, role):
        """Update a role with a json-patch."""
        data = request.get_json(force=True)
        data = apply_patch({'name': role.name}, data, True)
        with db.session.begin_nested():
            for key, value in data.items():
                setattr(role, key, value)
            db.session.merge(role)
        db.session.commit()
        return self.make_response(role, 200)

    @pass_role
    @need_role_permission('delete_role_permission_factory')
    def delete(self, role):
        """Delete a role."""
        role_to_delete_id = role.id
        if Role.query.filter_by(id=role_to_delete_id).count():
            db.session.delete(role)
            db.session.commit()
            return self.make_response(role, 204)
        else:
            raise ValueError("Cannot find role.")


def pass_user(with_roles=False):
    """Decorator to retrieve a user."""
    def pass_user_decorator(f):
        @wraps(f)
        def inner(self, user_id, *args, **kwargs):
            options = []
            if with_roles:
                options.append(orm.joinedload('roles'))

            if 'invenio-userprofiles' in current_app.extensions:
                options.append(orm.joinedload('profile'))

            user_query = User.query
            if options:
                user_query = user_query.options(*options)
            user = user_query.filter(User.id == user_id).one_or_none()

            if user is None:
                abort(404)
            return f(self, user=user, *args, **kwargs)
        return inner
    return pass_user_decorator


def verify_reassign_role_permission(permission_factory, role, user):
    """Check that the current user has permissions on reassigning a role.

    In case the permission check fails, an Flask abort is launched.
    If the user was previously logged-in, a HTTP error 403 is returned.
    Otherwise, is returned a HTTP error 401.

    :param permission_factory: permission factory used to check permissions.
    :param role: role with limited access.
    :param user: user with limited access.
    """
    # Note, cannot be done in one line due overloading of boolean
    # operations permission object.
    with current_app.app_context():
        if not permission_factory((role, user)).can():
            from flask_login import current_user
            if not current_user.is_authenticated:
                abort(401)
            abort(403)


def need_reassign_role_permission(factory_name):
    """Decorator checking that the user has permissions on reassigning a role.

    :param factory_name: name of the factory to retrieve.
    """
    def need_reassign_role_permission_builder(f):
        @wraps(f)
        def need_reassign_role_permission_decorator(self, role=None, user=None,
                                                    *args, **kwargs):
            permission_factory = getattr(current_accounts_rest,
                                         factory_name)()
            request._methodview = self
            if permission_factory:
                verify_reassign_role_permission(permission_factory, role, user)

            return f(self, role=role, user=user, *args, **kwargs)
        return need_reassign_role_permission_decorator
    return need_reassign_role_permission_builder


class AssignRoleResource(ContentNegotiatedMethodView):
    """Assign role resource."""

    view_name = 'assign_role'

    def __init__(self, **kwargs):
        """Constructor."""
        super(AssignRoleResource, self).__init__(
            serializers={
                'application/json': status_code_serializer
            },
            default_media_type='application/json',
            **kwargs
        )

    @pass_user(with_roles=True)
    @pass_role
    @need_reassign_role_permission('assign_role_permission_factory')
    def put(self, user, role):
        """Assign role to an user."""
        _datastore.add_role_to_user(user, role)
        return self.make_response(200)


class UnassignRoleResource(ContentNegotiatedMethodView):
    """Unassign role resource."""

    view_name = 'unassign_role'

    def __init__(self, **kwargs):
        """Constructor."""
        super(UnassignRoleResource, self).__init__(
            serializers={
                'application/json': status_code_serializer
            },
            default_media_type='application/json',
            **kwargs
        )

    @pass_user(with_roles=True)
    @pass_role
    @need_reassign_role_permission('unassign_role_permission_factory')
    def delete(self, user, role):
        """Remove role from a user."""
        _datastore.remove_role_from_user(user, role)
        return self.make_response(204)


def verify_user_permission(permission_factory, user):
    """Check that the current user has the required permissions on a user.

    In case the permission check fails, an Flask abort is launched.
    If the user was previously logged-in, a HTTP error 403 is returned.
    Otherwise, is returned a HTTP error 401.

    :param permission_factory: permission factory used to check permissions.
    :param user: user with limited access.
    """
    # Note, cannot be done in one line due overloading of boolean
    # operations permission object.
    with current_app.app_context():
        if not permission_factory(user=user).can():
            from flask_login import current_user
            if not current_user.is_authenticated:
                abort(401)
            abort(403)


def need_user_permission(factory_name):
    """Decorator checking that the user has the required permissions on role.

    :param factory_name: name of the factory to retrieve.
    """
    def need_user_permission_builder(f):
        @wraps(f)
        def need_user_permission_decorator(self, user=None, *args, **kwargs):
            permission_factory = getattr(current_accounts_rest,
                                         factory_name)()
            request._methodview = self
            if permission_factory:
                verify_user_permission(permission_factory, user)
            return f(self, user=user, *args, **kwargs)
        return need_user_permission_decorator
    return need_user_permission_builder


class UserRolesListResource(ContentNegotiatedMethodView):
    """User roles list resource."""

    view_name = 'user_roles_list'

    def __init__(self, max_result_window=None, **kwargs):
        """Constructor."""
        super(UserRolesListResource, self).__init__(
            serializers={
                'application/json': roles_list_serializer
            },
            default_media_type='application/json',
            **kwargs
        )
        self.max_result_window = max_result_window or 10000

    @pass_user()
    @need_user_permission('read_user_roles_list_permission_factory')
    def get(self, user):
        """Get a list of the user's roles."""
        page = request.values.get('page', 1, type=int)
        size = request.values.get('size', 10, type=int)
        if page * size >= self.max_result_window:
            raise MaxResultWindowRESTError()

        query_string = request.args.get('q')
        roles_query = db.session.query(Role).join(userrole).filter_by(
            user_id=user.id
        ).order_by(Role.name)
        total_query = db.session.query(
            func.count(Role.id)).join(userrole).filter_by(
                user_id=user.id
            )
        if query_string is not None:
            query_filter = Role.name.like('%{}%'.format(query_string))
            roles_query = roles_query.filter(query_filter)
            total_query = total_query.filter(query_filter)
        user_roles = roles_query.slice((page - 1) * size, page * size).all()
        total = total_query.scalar()

        result = self.make_response(
            roles=user_roles,
            total=total,
            links=paginated_query_links(
                'invenio_accounts_rest.user_roles_list',
                total, page, size,
                self.max_result_window,
                user_id=user.id),
            code=200,
        )

        return result


class UserAccountResource(ContentNegotiatedMethodView):
    """User account resource."""

    view_name = 'user'

    def __init__(self, **kwargs):
        """Constructor."""
        self.loaders = kwargs.get(
            'loaders',
            current_app.config.get(
                'ACCOUNTS_REST_ACCOUNT_LOADERS', {
                    'application/json': default_json_loader_without_profile,
                    'application/json-patch+json':
                        default_json_patch_loader_without_profile,
                }
                if 'invenio-userprofiles' not in current_app.extensions else {
                    'application/json': default_json_loader_with_profile,
                    'application/json-patch+json':
                    default_json_patch_loader_with_profile,
                }
            )
        )
        kwargs.setdefault(
            'serializers',
            current_app.config.get(
                'ACCOUNTS_REST_ACCOUNT_SERIALIZERS', {
                    'application/json': user_serializer,
                }
                if 'invenio-userprofiles' not in current_app.extensions else {
                    'application/json': user_with_profile_serializer,
                }
            )
        )
        kwargs.setdefault('default_media_type', 'application/json')
        super(UserAccountResource, self).__init__(
            **kwargs
        )

    @pass_user()
    @need_user_permission('read_user_properties_permission_factory')
    def get(self, user):
        """Get a user's properties."""
        return self.make_response(user, 200)

    @pass_user()
    @need_user_permission('update_user_properties_permission_factory')
    def patch(self, user):
        """Update a user's properties."""
        content_type = request.headers.get('Content-Type')
        loader = self.loaders.get(content_type)
        if loader is None:
            abort(406)
        data = loader(user=user)

        if data.get('password'):
            try:
                old_password = data['old_password']
            except KeyError:
                raise MissingOldPasswordError()
            updated_password = data['password']
            if verify_password(data['old_password'], user.password):
                user.password = encrypt_password(updated_password)
                db.session.commit()
                _datastore.put(user)
                password_changed.send(current_app._get_current_object(),
                                      user=user)
            del data['password']
            del data['old_password']

        user = User(id=user.id, **data)
        user = db.session.merge(user)
        db.session.commit()

        return self.make_response(user, 200)


class UserListResource(ContentNegotiatedMethodView):
    """Users list resource."""

    view_name = 'users_list'

    def __init__(self, max_result_window=None, **kwargs):
        """Constructor."""
        kwargs.setdefault(
            'serializers',
            current_app.config.get(
                'ACCOUNTS_REST_ACCOUNT_SERIALIZERS', {
                    'application/json': users_list_serializer
                }
                if 'invenio-userprofiles' not in current_app.extensions else {
                    'application/json': users_with_profile_list_serializer
                }
            )
        )
        kwargs.setdefault('default_media_type', 'application/json')
        super(UserListResource, self).__init__(
            **kwargs
        )
        self.max_result_window = max_result_window or 10000

    @require_api_auth()
    @need_list_permission('read_users_list_permission_factory')
    def get(self):
        """Get accounts/users/?q=."""
        page = request.values.get('page', 1, type=int)
        size = request.values.get('size', 10, type=int)
        if page * size >= self.max_result_window:
            raise MaxResultWindowRESTError()

        if 'invenio-userprofiles' not in current_app.extensions:
            users_query = User.query
        else:
            users_query = User.query.options(orm.joinedload('profile'))

        total_query = db.session.query(func.count(User.id))

        query_string = request.args.get('q')
        if query_string is not None:
            query_filter = User.email.like(
                '%{}%'.format(query_string)
            ) | (cast(User.id, String) == query_string)
            users_query = users_query.filter(query_filter)
            total_query = total_query.filter(query_filter)

        users = users_query.order_by(User.email).slice(
            (page - 1) * size, page * size
        ).all()
        total = total_query.scalar()

        result = self.make_response(
            users=users,
            total=total,
            links=paginated_query_links('invenio_accounts_rest.users_list',
                                        total, page, size,
                                        self.max_result_window),
            code=200,
        )
        return result


blueprint.add_url_rule(
    '/roles',
    view_func=RolesListResource.as_view(
        RolesListResource.view_name
    )
)

blueprint.add_url_rule(
    '/roles/<string:role_id>',
    view_func=RoleResource.as_view(
        RoleResource.view_name
    )
)


blueprint.add_url_rule(
    '/roles/<string:role_id>/users/<string:user_id>',
    view_func=AssignRoleResource.as_view(
        AssignRoleResource.view_name
    )
)


blueprint.add_url_rule(
    '/roles/<string:role_id>/users/<string:user_id>',
    view_func=UnassignRoleResource.as_view(
        UnassignRoleResource.view_name
    )
)


blueprint.add_url_rule(
    '/users/<string:user_id>/roles',
    view_func=UserRolesListResource.as_view(
        UserRolesListResource.view_name
    )
)


blueprint.add_url_rule(
    '/users/<string:user_id>',
    view_func=UserAccountResource.as_view(
        UserAccountResource.view_name
    )
)


blueprint.add_url_rule(
    '/users',
    view_func=UserListResource.as_view(
        UserListResource.view_name
    )
)
