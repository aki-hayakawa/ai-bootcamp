# settings.py
KEYCLOAK_CONFIG = {
    'SERVER_URL': 'http://localhost:8080/auth/',
    'REALM_NAME': 'ai-bootcamp',
    'CLIENT_ID': 'frontend-client',
    'CLIENT_SECRET': 'nr1hzXeZHBNoO7vx513Kk4wdIbbWDH9q',
    'REDIRECT_URI': 'http://localhost:8000/callback/'
}

# views.py
from django.shortcuts import redirect
from django.conf import settings
from keycloak import KeycloakOpenID

def login(request):
    keycloak_openid = KeycloakOpenID(
        server_url=settings.KEYCLOAK_CONFIG['SERVER_URL'],
        client_id=settings.KEYCLOAK_CONFIG['CLIENT_ID'],
        realm_name=settings.KEYCLOAK_CONFIG['REALM_NAME'],
        client_secret_key=settings.KEYCLOAK_CONFIG['CLIENT_SECRET']
    )
    
    auth_url = keycloak_openid.auth_url(
        redirect_uri=settings.KEYCLOAK_CONFIG['REDIRECT_URI'],
        scope="openid",
        state="some_state"
    )
    
    return redirect(auth_url)

def callback(request):
    code = request.GET.get('code')
    
    keycloak_openid = KeycloakOpenID(
        server_url=settings.KEYCLOAK_CONFIG['SERVER_URL'],
        client_id=settings.KEYCLOAK_CONFIG['CLIENT_ID'],
        realm_name=settings.KEYCLOAK_CONFIG['REALM_NAME'],
        client_secret_key=settings.KEYCLOAK_CONFIG['CLIENT_SECRET']
    )
    
    tokens = keycloak_openid.token(
        grant_type="authorization_code",
        code=code,
        redirect_uri=settings.KEYCLOAK_CONFIG['REDIRECT_URI']
    )
    
    # Store tokens in session
    request.session['access_token'] = tokens['access_token']
    request.session['refresh_token'] = tokens['refresh_token']
    
    # Get user info
    userinfo = keycloak_openid.userinfo(request.session['access_token'])
    request.session['userinfo'] = userinfo
    
    return redirect('protected')

def protected(request):
    if 'access_token' not in request.session:
        return redirect('login')
    
    return HttpResponse(f"Hello {request.session['userinfo']['preferred_username']}!")