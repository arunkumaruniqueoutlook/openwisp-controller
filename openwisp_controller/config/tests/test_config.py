from copy import deepcopy

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.transaction import atomic
from django.test import TestCase
from netjsonconfig import OpenWrt
from swapper import load_model

from openwisp_users.tests.utils import TestOrganizationMixin
from openwisp_utils.tests import catch_signal

from .. import settings as app_settings
from ..signals import config_modified, config_status_changed
from .utils import CreateConfigTemplateMixin, TestVpnX509Mixin

Config = load_model('config', 'Config')
Device = load_model('config', 'Device')
Template = load_model('config', 'Template')
Vpn = load_model('config', 'Vpn')
Ca = load_model('django_x509', 'Ca')


class TestConfig(
    CreateConfigTemplateMixin, TestOrganizationMixin, TestVpnX509Mixin, TestCase
):
    """
    tests for Config model
    """

    fixtures = ['test_templates']
    maxDiff = None

    def test_str(self):
        c = Config()
        self.assertEqual(str(c), str(c.pk))
        c = Config(device=Device(name='test'))
        self.assertEqual(str(c), 'test')

    def test_config_not_none(self):
        c = Config(
            device=self._create_device(), backend='netjsonconfig.OpenWrt', config=None
        )
        c.full_clean()
        self.assertEqual(c.config, {})

    def test_backend_class(self):
        c = Config(backend='netjsonconfig.OpenWrt')
        self.assertIs(c.backend_class, OpenWrt)

    def test_backend_instance(self):
        config = {'general': {'hostname': 'config'}}
        c = Config(backend='netjsonconfig.OpenWrt', config=config)
        self.assertIsInstance(c.backend_instance, OpenWrt)

    def test_netjson_validation(self):
        config = {'interfaces': {'invalid': True}}
        c = Config(
            device=self._create_device(), backend='netjsonconfig.OpenWrt', config=config
        )
        # ensure django ValidationError is raised
        try:
            c.full_clean()
        except ValidationError as e:
            self.assertIn('Invalid configuration', e.message_dict['__all__'][0])
        else:
            self.fail('ValidationError not raised')

    def test_json(self):
        dhcp = Template.objects.get(name='dhcp')
        radio = Template.objects.get(name='radio0')
        c = self._create_config(
            organization=self._get_org(), config={'general': {'hostname': 'json-test'}}
        )
        c.templates.add(dhcp)
        c.templates.add(radio)
        full_config = {
            'general': {'hostname': 'json-test'},
            'interfaces': [
                {
                    'name': 'eth0',
                    'type': 'ethernet',
                    'addresses': [{'proto': 'dhcp', 'family': 'ipv4'}],
                }
            ],
            'radios': [
                {
                    'name': 'radio0',
                    'phy': 'phy0',
                    'driver': 'mac80211',
                    'protocol': '802.11n',
                    'channel': 11,
                    'channel_width': 20,
                    'tx_power': 8,
                    'country': 'IT',
                }
            ],
        }
        del c.backend_instance
        self.assertDictEqual(c.json(dict=True), full_config)
        json_string = c.json()
        self.assertIn('json-test', json_string)
        self.assertIn('eth0', json_string)
        self.assertIn('radio0', json_string)

    def test_m2m_validation(self):
        # if config and template have a conflicting non-unique item
        # that violates the schema, the system should not allow
        # the assignment and raise an exception
        config = {'files': [{'path': '/test', 'mode': '0644', 'contents': 'test'}]}
        config_copy = deepcopy(config)
        t = Template(name='files', backend='netjsonconfig.OpenWrt', config=config)
        t.full_clean()
        t.save()
        c = self._create_config(organization=self._get_org(), config=config_copy)
        with atomic():
            try:
                c.templates.add(t)
            except ValidationError:
                self.fail('ValidationError raised!')
        t.config['files'][0]['path'] = '/test2'
        t.full_clean()
        t.save()
        c.templates.add(t)

    def test_checksum(self):
        c = self._create_config(organization=self._get_org())
        self.assertEqual(len(c.checksum), 32)

    def test_backend_import_error(self):
        """
        see issue #5
        https://github.com/openwisp/django-netjsonconfig/issues/5
        """
        c = Config(device=self._create_device())
        with self.assertRaises(ValidationError):
            c.full_clean()
        c.backend = 'wrong'
        with self.assertRaises(ValidationError):
            c.full_clean()

    def test_default_status(self):
        c = Config()
        self.assertEqual(c.status, 'modified')

    def test_status_modified_after_change(self):
        c = self._create_config(organization=self._get_org(), status='applied')
        self.assertEqual(c.status, 'applied')
        c.refresh_from_db()
        c.config = {'general': {'description': 'test'}}
        c.full_clean()
        c.save()
        self.assertEqual(c.status, 'modified')

    def test_status_modified_after_templates_changed(self):
        c = self._create_config(organization=self._get_org(), status='applied')
        self.assertEqual(c.status, 'applied')
        t = Template.objects.first()
        c.templates.add(t)
        c.refresh_from_db()
        self.assertEqual(c.status, 'modified')
        c.status = 'applied'
        c.save()
        c.refresh_from_db()
        self.assertEqual(c.status, 'applied')
        c.templates.remove(t)
        c.refresh_from_db()
        self.assertEqual(c.status, 'modified')

    def test_status_modified_after_context_changed(self):
        c = self._create_config(organization=self._get_org(), status='applied')
        self.assertEqual(c.status, 'applied')
        c.refresh_from_db()
        c.context = {'lan_ipv4': '192.168.40.1'}
        c.full_clean()
        c.save()
        self.assertEqual(c.status, 'modified')

    def test_auto_hostname(self):
        c = self._create_config(device=self._create_device(name='automate-me'))
        expected = {'general': {'hostname': 'automate-me'}}
        self.assertDictEqual(c.backend_instance.config, expected)
        c.refresh_from_db()
        self.assertDictEqual(c.config, {'general': {}})

    def test_config_context(self):
        config = {
            'general': {
                'id': '{{ id }}',
                'key': '{{ key }}',
                'name': '{{ name }}',
                'mac_address': '{{ mac_address }}',
            }
        }
        c = Config(
            device=self._create_device(name='context-test'),
            backend='netjsonconfig.OpenWrt',
            config=config,
        )
        output = c.backend_instance.render()
        self.assertIn(str(c.device.id), output)
        self.assertIn(c.device.key, output)
        self.assertIn(c.device.name, output)
        self.assertIn(c.device.mac_address, output)

    def test_context_validation(self):
        config = Config(
            device=self._create_device(name='context-test'),
            backend='netjsonconfig.OpenWrt',
            config={},
        )

        for value in [None, '', False]:
            with self.subTest(f'testing {value} in config.context'):
                config.context = value
                config.full_clean()
                self.assertEqual(config.context, {})

        for value in [['a', 'b'], '"test"']:
            with self.subTest(
                f'testing {value} in config.context, expecting validation error'
            ):
                config.context = value
                with self.assertRaises(ValidationError) as context_manager:
                    config.full_clean()
                message_dict = context_manager.exception.message_dict
                self.assertIn('context', message_dict)
                self.assertIn(
                    'the supplied value is not a JSON object', message_dict['context']
                )

    def test_context_setting(self):
        config = {'general': {'vpnserver1': '{{ vpnserver1 }}'}}
        c = Config(
            device=self._create_device(), backend='netjsonconfig.OpenWrt', config=config
        )
        output = c.backend_instance.render()
        vpnserver1 = settings.OPENWISP_CONTROLLER_CONTEXT['vpnserver1']
        self.assertIn(vpnserver1, output)

    def test_mac_address_as_hostname(self):
        c = self._create_config(device=self._create_device(name='00:11:22:33:44:55'))
        self.assertIn('00-11-22-33-44-55', c.backend_instance.render())

    def test_create_vpnclient(self):
        vpn = self._create_vpn()
        t = self._create_template(name='test-network', type='vpn', vpn=vpn)
        c = self._create_config(device=self._create_device(name='test-create-cert'))
        c.templates.add(t)
        c.save()
        vpnclient = c.vpnclient_set.first()
        self.assertIsNotNone(vpnclient)
        self.assertEqual(c.vpnclient_set.count(), 1)
        self.assertEqual(vpnclient.config, c)
        self.assertEqual(vpnclient.vpn, vpn)

    def test_delete_vpnclient(self):
        self.test_create_vpnclient()
        c = Config.objects.get(device__name='test-create-cert')
        t = Template.objects.get(name='test-network')
        c.templates.remove(t)
        c.save()
        vpnclient = c.vpnclient_set.first()
        self.assertIsNone(vpnclient)
        self.assertEqual(c.vpnclient_set.count(), 0)

    def test_clear_vpnclient(self):
        self.test_create_vpnclient()
        c = Config.objects.get(device__name='test-create-cert')
        c.templates.clear()
        c.save()
        vpnclient = c.vpnclient_set.first()
        self.assertIsNone(vpnclient)
        self.assertEqual(c.vpnclient_set.count(), 0)

    def test_create_cert(self):
        vpn = self._create_vpn()
        t = self._create_template(
            name='test-create-cert', type='vpn', vpn=vpn, auto_cert=True
        )
        c = self._create_config(device=self._create_device(name='test-create-cert'))
        c.templates.add(t)
        vpnclient = c.vpnclient_set.first()
        self.assertIsNotNone(vpnclient)
        self.assertTrue(vpnclient.auto_cert)
        self.assertIsNotNone(vpnclient.cert)
        self.assertEqual(c.vpnclient_set.count(), 1)

    def test_automatically_created_cert_common_name_format(self):
        self.test_create_cert()
        c = Config.objects.get(device__name='test-create-cert')
        vpnclient = c.vpnclient_set.first()
        expected_cn = app_settings.COMMON_NAME_FORMAT.format(**c.device.__dict__)
        self.assertEqual(vpnclient.cert.common_name, expected_cn)

    def test_automatically_created_cert_deleted_post_clear(self):
        self.test_create_cert()
        c = Config.objects.get(device__name='test-create-cert')
        vpnclient = c.vpnclient_set.first()
        cert = vpnclient.cert
        cert_model = cert.__class__
        c.templates.clear()
        self.assertEqual(c.vpnclient_set.count(), 0)
        self.assertEqual(cert_model.objects.filter(pk=cert.pk).count(), 0)

    def test_automatically_created_cert_deleted_post_remove(self):
        self.test_create_cert()
        c = Config.objects.get(device__name='test-create-cert')
        t = Template.objects.get(name='test-create-cert')
        vpnclient = c.vpnclient_set.first()
        cert = vpnclient.cert
        cert_model = cert.__class__
        c.templates.remove(t)
        self.assertEqual(c.vpnclient_set.count(), 0)
        self.assertEqual(cert_model.objects.filter(pk=cert.pk).count(), 0)

    def test_create_cert_false(self):
        vpn = self._create_vpn()
        t = self._create_template(type='vpn', auto_cert=False, vpn=vpn)
        c = self._create_config(device=self._create_device(name='test-create-cert'))
        c.templates.add(t)
        c.save()
        vpnclient = c.vpnclient_set.first()
        self.assertIsNotNone(vpnclient)
        self.assertFalse(vpnclient.auto_cert)
        self.assertIsNone(vpnclient.cert)
        self.assertEqual(c.vpnclient_set.count(), 1)

    def _get_vpn_context(self):
        self.test_create_cert()
        c = Config.objects.get(device__name='test-create-cert')
        context = c.get_context()
        vpnclient = c.vpnclient_set.first()
        return context, vpnclient

    def test_vpn_context_ca_path(self):
        context, vpnclient = self._get_vpn_context()
        ca = vpnclient.cert.ca
        key = 'ca_path_{0}'.format(vpnclient.vpn.pk.hex)
        filename = 'ca-{0}-{1}.pem'.format(ca.pk, ca.common_name)
        value = '{0}/{1}'.format(app_settings.CERT_PATH, filename)
        self.assertIn(key, context)
        self.assertIn(value, context[key])

    def test_vpn_context_ca_path_bug(self):
        vpn = self._create_vpn(ca_options={'common_name': 'common name CA'})
        t = self._create_template(type='vpn', auto_cert=True, vpn=vpn)
        c = self._create_config(device=self._create_device(name='test-create-cert'))
        c.templates.add(t)
        context = c.get_context()
        ca = vpn.ca
        key = 'ca_path_{0}'.format(vpn.pk.hex)
        filename = 'ca-{0}-{1}.pem'.format(ca.pk, ca.common_name.replace(' ', '_'))
        value = '{0}/{1}'.format(app_settings.CERT_PATH, filename)
        self.assertIn(key, context)
        self.assertIn(value, context[key])

    def test_vpn_context_ca_contents(self):
        context, vpnclient = self._get_vpn_context()
        key = 'ca_contents_{0}'.format(vpnclient.vpn.pk.hex)
        value = vpnclient.cert.ca.certificate
        self.assertIn(key, context)
        self.assertIn(value, context[key])

    def test_vpn_context_cert_path(self):
        context, vpnclient = self._get_vpn_context()
        vpn_pk = vpnclient.vpn.pk.hex
        key = 'cert_path_{0}'.format(vpn_pk)
        filename = 'client-{0}.pem'.format(vpn_pk)
        value = '{0}/{1}'.format(app_settings.CERT_PATH, filename)
        self.assertIn(key, context)
        self.assertIn(value, context[key])

    def test_vpn_context_cert_contents(self):
        context, vpnclient = self._get_vpn_context()
        vpn_pk = vpnclient.vpn.pk.hex
        key = 'cert_contents_{0}'.format(vpn_pk)
        value = vpnclient.cert.certificate
        self.assertIn(key, context)
        self.assertIn(value, context[key])

    def test_vpn_context_key_path(self):
        context, vpnclient = self._get_vpn_context()
        vpn_pk = vpnclient.vpn.pk.hex
        key = 'key_path_{0}'.format(vpn_pk)
        filename = 'key-{0}.pem'.format(vpn_pk)
        value = '{0}/{1}'.format(app_settings.CERT_PATH, filename)
        self.assertIn(key, context)
        self.assertIn(value, context[key])

    def test_vpn_context_key_contents(self):
        context, vpnclient = self._get_vpn_context()
        vpn_pk = vpnclient.vpn.pk.hex
        key = 'key_contents_{0}'.format(vpn_pk)
        value = vpnclient.cert.private_key
        self.assertIn(key, context)
        self.assertIn(value, context[key])

    def test_vpn_context_no_cert(self):
        vpn = self._create_vpn()
        t = self._create_template(type='vpn', auto_cert=False, vpn=vpn)
        c = self._create_config(device=self._create_device(name='test-create-cert'))
        c.templates.add(t)
        c.save()
        context = c.get_context()
        vpn_id = vpn.pk.hex
        cert_path_key = 'cert_path_{0}'.format(vpn_id)
        cert_contents_key = 'cert_contents_{0}'.format(vpn_id)
        key_path_key = 'key_path_{0}'.format(vpn_id)
        key_contents_key = 'key_contents_{0}'.format(vpn_id)
        ca_path_key = 'ca_path_{0}'.format(vpn_id)
        ca_contents_key = 'ca_contents_{0}'.format(vpn_id)
        self.assertNotIn(cert_path_key, context)
        self.assertNotIn(cert_contents_key, context)
        self.assertNotIn(key_path_key, context)
        self.assertNotIn(key_contents_key, context)
        self.assertIn(ca_path_key, context)
        self.assertIn(ca_contents_key, context)

    def test_m2m_str_conversion(self):
        t = self._create_template()
        c = self._create_config(device=self._create_device(name='test-m2m-str-repr'))
        c.templates.add(t)
        c.save()
        through = str(c.templates.through.objects.first())
        self.assertIn('Relationship with', through)
        self.assertIn(t.name, through)

    def test_get_template_model_static(self):
        self.assertIs(Config.get_template_model(), Template)

    def test_get_template_model_bound(self):
        self.assertIs(Config().get_template_model(), Template)

    def test_remove_duplicate_files(self):
        template1 = self._create_template(
            name='test-vpn-1',
            config={
                'files': [
                    {
                        'path': '/etc/vpnserver1',
                        'mode': '0644',
                        'contents': '{{ name }}\n{{ vpnserver1 }}\n',
                    }
                ]
            },
        )
        template2 = self._create_template(
            name='test-vpn-2',
            config={
                'files': [
                    {
                        'path': '/etc/vpnserver1',
                        'mode': '0644',
                        'contents': '{{ name }}\n{{ vpnserver1 }}\n',
                    }
                ]
            },
        )
        config = self._create_config(organization=self._get_org())
        config.templates.add(template1)
        config.templates.add(template2)
        config.refresh_from_db()
        try:
            result = config.get_backend_instance(
                template_instances=[template1, template2]
            ).render()
        except ValidationError:
            self.fail('ValidationError raised!')
        else:
            self.assertIn('# path: /etc/vpnserver1', result)

    def test_duplicated_files_in_config(self):
        try:
            self._create_config(
                organization=self._get_org(),
                config={
                    'files': [
                        {
                            'path': '/etc/vpnserver1',
                            'mode': '0644',
                            'contents': '{{ name }}\n{{ vpnserver1 }}\n',
                        },
                        {
                            'path': '/etc/vpnserver1',
                            'mode': '0644',
                            'contents': '{{ name }}\n{{ vpnserver1 }}\n',
                        },
                    ]
                },
            )
        except ValidationError as e:
            self.assertIn('Invalid configuration triggered by "#/files"', str(e))
        else:
            self.fail('ValidationError not raised!')

    def test_config_with_shared_template(self):
        org = self._get_org()
        config = self._create_config(organization=org)
        # shared template
        template = self._create_template()
        # add shared template
        config.templates.add(template)
        self.assertIsNone(template.organization)
        self.assertEqual(config.templates.first().pk, template.pk)

    def test_config_and_template_different_organization(self):
        org1 = self._get_org()
        org2 = self._create_org(name='test org2', slug='test-org2')
        template = self._create_template(organization=org1)
        config = self._create_config(organization=org2)
        try:
            config.templates.add(template)
        except ValidationError as e:
            self.assertIn('do not match the organization', e.messages[0])
        else:
            self.fail('ValidationError not raised')

    def test_config_status_changed_not_sent_on_creation(self):
        org = self._get_org()
        with catch_signal(config_status_changed) as handler:
            self._create_config(organization=org)
            handler.assert_not_called()

    def test_config_status_changed_modified(self):
        org = self._get_org()
        with catch_signal(config_status_changed) as handler:
            c = self._create_config(organization=org, status='applied')
            handler.assert_not_called()
            self.assertEqual(c.status, 'applied')

        with catch_signal(config_status_changed) as handler:
            c.config = {'general': {'description': 'test'}}
            c.full_clean()
            self.assertEqual(c.status, 'modified')
            handler.assert_not_called()

        with catch_signal(config_status_changed) as handler:
            c.save()
            handler.assert_called_once_with(
                sender=Config, signal=config_status_changed, instance=c,
            )
            self.assertEqual(c.status, 'modified')

        with catch_signal(config_status_changed) as handler:
            c.config = {'general': {'description': 'changed again'}}
            c.full_clean()
            c.save()
            handler.assert_not_called()
            self.assertEqual(c.status, 'modified')

    def test_config_modified_sent(self):
        org = self._get_org()
        with catch_signal(config_modified) as handler:
            c = self._create_config(organization=org, status='applied')
            handler.assert_not_called()
            self.assertEqual(c.status, 'applied')

        with catch_signal(config_modified) as handler:
            c.config = {'general': {'description': 'test'}}
            c.full_clean()
            handler.assert_not_called()
            self.assertEqual(c.status, 'modified')

        with catch_signal(config_modified) as handler:
            c.config = {'general': {'description': 'hey'}}
            c.full_clean()
            handler.assert_not_called()

        with catch_signal(config_modified) as handler:
            c.save()
            handler.assert_called_once_with(
                sender=Config,
                signal=config_modified,
                instance=c,
                device=c.device,
                config=c,
            )
            self.assertEqual(c.status, 'modified')

        with catch_signal(config_modified) as handler:
            c.config = {'general': {'description': 'changed again'}}
            c.full_clean()
            c.full_clean()
            c.save()
            handler.assert_called_once()
            self.assertEqual(c.status, 'modified')
