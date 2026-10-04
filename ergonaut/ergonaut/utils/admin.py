from django.db.models.fields.related import RelatedField


def register(model):
    """
    To make the admin more performant, we ensure all the the relations
    are listed under raw_id_fields
    """

    def decorator(modeladmin):
        raw_id_fields = []
        for field in model._meta.fields:
            if isinstance(field, RelatedField):
                raw_id_fields.append(field.name)
        setattr(modeladmin, "raw_id_fields", raw_id_fields)
        return admin_site.register(model, modeladmin)

    return decorator


from django.contrib.admin import AdminSite


class CustomAdmin(AdminSite):
    # Text to put at the end of each page's <title>.
    site_title = "ergonaut"

    # Text to put in each page's <h1> (and above login form).
    site_header = "ergonaut"

    def login(self, request, extra_context=None):
        """The admin login, with the same failed-login limit as the API's."""
        from django.http import HttpResponse

        from ergonaut.utils.throttle import login_blocked, login_failed, login_succeeded

        username = request.POST.get("username", "") if request.method == "POST" else ""
        if username and login_blocked(request, username):
            return HttpResponse("Too many failed logins; try again later.", status=429, content_type="text/plain")
        response = super().login(request, extra_context)
        if username:
            if response.status_code == 302:  # logged in, sent on
                login_succeeded(request, username)
            else:
                login_failed(request, username)
        return response


admin_site = CustomAdmin()
