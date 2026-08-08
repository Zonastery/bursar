{{ fullname | escape | underline}}

.. currentmodule:: {{ fullname }}

.. automodule:: {{ fullname }}

{% if attributes %}
Module attributes
-----------------

.. autosummary::
   :class: api-autosummary-generation
   :toctree: .
{% for item in attributes %}
   {{ item }}
{%- endfor %}

{% for item in attributes %}:doc:`{{ item }} <{{ fullname }}.{{ item }}>`{% if not loop.last %} · {% endif %}{% endfor %}
{% endif %}

{% if functions %}
Functions
---------

.. autosummary::
   :class: api-autosummary-generation
   :toctree: .
{% for item in functions %}
   {{ item }}
{%- endfor %}

{% for item in functions %}:doc:`{{ item }} <{{ fullname }}.{{ item }}>`{% if not loop.last %} · {% endif %}{% endfor %}
{% endif %}

{% if classes %}
Classes
-------

.. autosummary::
   :class: api-autosummary-generation
   :toctree: .
{% for item in classes %}
   {{ item }}
{%- endfor %}

{% for item in classes %}:doc:`{{ item }} <{{ fullname }}.{{ item }}>`{% if not loop.last %} · {% endif %}{% endfor %}
{% endif %}

{% if exceptions %}
Exceptions
----------

.. autosummary::
   :class: api-autosummary-generation
   :toctree: .
{% for item in exceptions %}
   {{ item }}
{%- endfor %}

{% for item in exceptions %}:doc:`{{ item }} <{{ fullname }}.{{ item }}>`{% if not loop.last %} · {% endif %}{% endfor %}
{% endif %}

{% if modules %}
Modules
-------

.. autosummary::
   :class: api-autosummary-generation
   :toctree: .
   :recursive:
{% for item in modules %}
   {{ item }}
{%- endfor %}

{% for item in modules %}:doc:`{{ item }} <{{ item }}>`{% if not loop.last %} · {% endif %}{% endfor %}
{% endif %}
