from django.views.generic import TemplateView


class MyView(TemplateView):
    template_name = "django_ergo/my_template.html"
