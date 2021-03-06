from .models import RecentlyUpdatedMixin, AccountRequest
from firecares.firestation.models import FireDepartment

from datetime import timedelta
from urlparse import urlsplit, urlunsplit
from django.contrib.auth import get_user_model, authenticate
from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.core.urlresolvers import reverse, resolve
from django.test import Client, TestCase
from django.utils import timezone
from bs4 import BeautifulSoup

User = get_user_model()


class CoreTests(TestCase):
    fixtures = ['test_forgot.json']

    def test_sitemap(self):
        """
        Ensures the generated sitemap has correct priorities
        """

        FireDepartment.objects.create(name='testy2', population=2, featured=True)
        FireDepartment.objects.create(name='testy3', population=3)
        FireDepartment.objects.create(name='testy4', population=4)

        c = Client()
        response = c.get('/sitemap.xml')
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, 'xml')
        sitemap_list = soup.find_all('url')
        self.assertEqual(len(sitemap_list), 3 + 6)  # 3 test departments and 6 set navigation pages
        # find the three elements
        for testy in sitemap_list:
            if 'testy2' in testy.loc.get_text():
                testy2 = testy
            elif 'testy3' in testy.loc.get_text():
                testy3 = testy
            elif 'testy4' in testy.loc.get_text():
                testy4 = testy
        # assert that testy2 has higher priority than testy4 (because its featured) and 4 has more than 3
        self.assertGreater(float(testy2.priority.get_text()), float(testy4.priority.get_text()))
        self.assertGreater(float(testy4.priority.get_text()), float(testy3.priority.get_text()))

    def test_home_does_not_require_login(self):
        """
        Ensures the home page requires login.
        Note: This is just until we improve the home page.
        """

        c = Client()
        response = c.get('/')
        self.assertEqual(response.status_code, 200)

    def test_departments_requires_login(self):
        """
        Ensures the home page requires login.
        Note: This is just until we are out of closed beta.
        """

        c = Client()
        response = c.get('/departments')
        self.assertEqual(response.status_code, 302)
        self.assertTrue('login' in response.url)

    def test_recently_updated(self):
        """
        Tests the Recently Updated Mixin.
        """

        rum = RecentlyUpdatedMixin()
        rum.modified = timezone.now() - timedelta(days=11)
        self.assertFalse(rum.recently_updated)

        rum.modified = timezone.now() - timedelta(days=10)
        self.assertTrue(rum.recently_updated)

    def test_add_user(self):
        """
        Tests the add_user command.
        """

        with self.assertRaises(User.DoesNotExist):
            User.objects.get(username='foo')

        call_command('add_user', 'foo', 'bar', 'foo@bar.com')

        user = authenticate(username='foo', password='bar')
        self.assertIsNotNone(user)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertTrue(user.is_active)

        call_command('add_user', 'foo_admin', 'bar', 'foo@bar.com', is_superuser=True)
        user = authenticate(username='foo_admin', password='bar')
        self.assertIsNotNone(user)
        self.assertTrue(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertTrue(user.is_active)

        call_command('add_user', 'foo_admin1', 'bar', 'foo@bar.com', is_staff=True, is_active=False)
        user = authenticate(username='foo_admin1', password='bar')
        self.assertIsNotNone(user)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_active)

        # Make sure no error is thrown when creating a user that already exists
        call_command('add_user', 'foo_admin1', 'bar', 'foo@bar.com', is_staff=True, is_active=False)

    def test_registration_views(self):
        """
        Tests the registration view.
        """

        c = Client()

        with self.settings(REGISTRATION_OPEN=True):
            response = c.get(reverse('registration_register'))
            self.assertTrue(response.status_code, 200)

            c.post(reverse('registration_register'), data={'username': 'test',
                                                           'password1': 'test',
                                                           'password2': 'test',
                                                           'email': 'test@example.com'
                                                           })

            self.assertTrue(len(mail.outbox), 1)
            user = User.objects.get(username='test')
            self.assertFalse(user.is_active)
            self.assertFalse(user.is_superuser)
            self.assertFalse(user.is_staff)
            self.assertFalse(user.registrationprofile.activation_key_expired())

            response = c.get(reverse('registration_activate', kwargs={'activation_key':
                                                                      user.registrationprofile.activation_key}))
            # A 302 here means the activation succeeded
            self.assertEqual(response.status_code, 302)

            user = User.objects.get(username='test')
            self.assertTrue(user.is_active)
            self.assertTrue(user.registrationprofile.activation_key_expired())
            self.assertFalse(user.is_superuser)
            self.assertFalse(user.is_staff)

            # Check a bad activation code
            response = c.get(reverse('registration_activate', kwargs={'activation_key': 'nowaysdfsdfs'}))

            # A 200 here means the activation failed
            self.assertEqual(response.status_code, 200)

        # Make sure the user is forwarded to the registration closed page when REGISTRATION_OPEN is False
        with self.settings(REGISTRATION_OPEN=False):
            response = c.get(reverse('registration_register'))
            self.assertTrue(response.status_code, 302)

            response = c.get(reverse('registration_register'), follow=True)
            self.assertTrue(response.status_code, 200)
            self.assertEqual(response.request['PATH_INFO'], '/accounts/register/closed/')

    def test_password_reset(self):
        """
        Tests the forgotten/reset password workflow.
        """

        c = Client()

        resp = c.get(reverse('password_reset'))
        self.assertTrue(resp.status_code, 200)

        resp = c.post(reverse('password_reset'), data={'email': 'test@example.com'})
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(len(mail.outbox), 1)

        token = resp.context[0]['token']
        uid = resp.context[0]['uid']

        # Grab the token and uidb64 so that we can hit the reset url
        resp = c.get(reverse('password_reset_confirm', kwargs={'token': token, 'uidb64': uid}))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.template_name.endswith('password_reset_confirm.html'))

        resp = c.post(reverse('password_reset_confirm', kwargs={'token': token, 'uidb64': uid}),
                      {'new_password1': 'mynewpassword', 'new_password2': 'mynewpassword'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resolve(urlsplit(resp.url).path).url_name, 'password_reset_complete')

        resp = c.post(reverse('login'), {'username': 'tester_mcgee', 'password': 'mynewpassword'})

        # User is returned to the login page on error vs redirected by default
        self.assertEqual(resp.status_code, 302)
        self.assertNotEqual(resolve(urlsplit(resp.url).path).url_name, 'login')

    def test_forgot_username(self):
        """
        Tests the forgot username workflow.
        """

        c = Client()
        resp = c.get(reverse('forgot_username'))
        self.assertEqual(resp.status_code, 200)

        resp = c.post(reverse('forgot_username'), {'email': 'test@example.com'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resolve(urlsplit(resp.url).path).url_name, 'username_sent')

        self.assertEqual(len(mail.outbox), 1)
        # Make sure that the username is actually in the email (otherwise what's the point?)
        self.assertTrue('tester_mcgee' in mail.outbox[0].body)

    def test_change_password(self):
        """
        Test the change password worflow.
        """

        c = Client()
        resp = c.get(reverse('password_change'))
        # Should redirect to login view since password_change requires a logged-in user
        self.assertEqual(resp.status_code, 302)

        # Weird behavior on this 302 to /login, it *should* have a trailing slash, but doesn't
        split = urlsplit(resp.url)
        shimmed = urlsplit(urlunsplit((split.scheme, split.netloc, split.path + '/', split.query, split.fragment)))
        self.assertEquals(resolve(shimmed.path).url_name, 'login')

        # Should redirect to the password_change page after login
        resp = c.post(shimmed.path + '?' + shimmed.query, {'username': 'tester_mcgee', 'password': 'test'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resolve(urlsplit(resp.url).path).url_name, 'password_change')

        # Fill out the change password form w/ invalid old password_change
        resp = c.post(reverse('password_change'), {'old_password': 'badpassword'})
        # No redirect means something bad happened
        self.assertEqual(resp.status_code, 200)

        # Fill out the change password form w/ new passwords that don't match
        resp = c.post(reverse('password_change'),
                      {'old_password': 'test',
                       'new_password1': 'brandnewpassword',
                       'new_password2': 'uhohthiswontmatch'})
        self.assertEqual(resp.status_code, 200)

        # Fill out using valid params that would change password
        resp = c.post(reverse('password_change'),
                      {'old_password': 'test',
                       'new_password1': 'newerpassword',
                       'new_password2': 'newerpassword'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resolve(urlsplit(resp.url).path).url_name, 'password_change_done')

    def test_contact_us(self):
        """
        Test the contact us form submission
        """

        c = Client()
        with self.settings(ADMINS=(('Test Admin', 'admin@example.com'),)):
            # Disable captcha checking
            settings.RECAPTCHA_SECRET = ''
            resp = c.post(reverse('contact_us'), {
                          'name': 'Tester McGee',
                          'email': 'test@example.com',
                          'message': 'This is a test'})

            self.assertRedirects(resp, reverse('contact_thank_you'))
            self.assertEqual(len(mail.outbox), 1)

    def test_display_404_page(self):
        c = Client()
        resp = c.get('/notarealpage/')
        self.assertContains(resp, 'Page not found', status_code=404)

    def test_request_login(self):
        """
        Tests the account request workflow.
        """
        view = 'account_request'
        c = Client()

        with self.settings(ADMINS=(('Test Admin', 'admin@example.com'),)):
            resp = c.post(reverse(view), {'email': 'test@example.com'}, follow=True)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(1, AccountRequest.objects.count())

            # Ensure duplicates are handled gracefully.
            resp = c.post(reverse(view), {'email': 'test@example.com'}, follow=True)
            self.assertEqual(resp.status_code, 200)

            # Make sure admin email is triggered.
            self.assertEqual(len(mail.outbox), 1)
            print mail.outbox[0].message()
