# Create your views here.
import datetime
import pytz
import time
import urllib2,urllib
import cookielib
import StringIO
import Cookie
import xml.etree.cElementTree as ElementTree
import threading
from django.http import HttpResponse
from django.http import HttpResponseForbidden
from follower.mapper import Mapper
from follower.models import Email
from follower.reachout import ReachOut
from django.template import Context,RequestContext,loader
from django.forms import ModelForm,IntegerField
from django.contrib.auth.decorators import login_required
from django.views.generic.edit import UpdateView,CreateView,DeleteView
from django.views.generic.list import ListView
from django.conf import settings

inptag = '{http://www.w3.org/1999/xhtml}input'
formtag = '{http://www.w3.org/1999/xhtml}form'

class MapperListView(ListView):
    """
    A view class for /mapper/list that displays a list of mappers
    """
    queryset = Mapper.objects.all().order_by('-edit_date')
    model = Mapper
    context_object_name = 'tracker_list'
    template_name = 'follower/list.html'


@login_required
def update_mappers(request):
    """
    If the mapppers changeset RSS feed hasn't been scanned
    in the last 24h we scan it for updates
    """
    yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    update_list = Mapper.objects.all().exclude(scan_date__gt=yesterday)

    def mapper_update():
        for mapper in update_list:
            mapper.check_edits()
            mapper.save()            
            #omit some HTML comments to prevent the connection from timing out
            #we also pause for 3 seconds to avoid overloading the OSM API
            yield '<!-- test --> '        
            time.sleep(3)
        yield '<html>updates complete</html>'
    response  = HttpResponse(mapper_update())    
    return response;


@login_required
def mapper_bulk_action(request):
    """
    Perform a bulk action to a list of mappers.
    This view is for dispatching bulk actions on a list of mappers
    """
    if request.GET.has_key('action') and request.GET['action']=='reach out':
        return reach_out(request)


@login_required
def reach_out(request):
    """
    Create a mapper reach out to a list of mappers.
    """
    tracker_list = Mapper.objects.all()
    selected = []
    for mapper in tracker_list:
            key = 'mapper_selected_' + str(mapper.id)
            if request.GET.has_key(key) and request.GET[key] == 'on':
                selected.append(mapper)

    emails = Email.objects.all()
    
    t = loader.get_template('follower/reach_out.html')
    
    c = RequestContext(request,{'mappers': selected,
                                'emails' : emails })
    return HttpResponse(t.render(c))


@login_required
def reach_out_create(request):
    """
    Create a mapper reach out in response to the requested actions.
    """
    email = None
    if request.POST.has_key('email'):
        email_id = request.POST['email']
        email = Email.objects.filter(id=email_id)
    count=0
    if request.POST.has_key('mapper_count'):
        count = int(request.POST['mapper_count'])
    cookies = authenticate_osm(request.POST['osm_user'],
                               request.POST['osm_password'])
    opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cookies))
    sent=[]
    failed=[]

    for idx in range(0,count):
        if request.POST.has_key('mapper_' + str(idx)):
            mapper_id = request.POST['mapper_' + str(idx)]
            mapper = Mapper.objects.filter(id=mapper_id)
            #first check to see if the email has been sent already.
            already_sent = ReachOut.objects.all().filter(email=email[0],
                                                         mapper=mapper[0])
            if len(already_sent) > 0:
                error = { 'mapper':mapper,
                          'reason':'already sent',
                          'code':0}
                failed.append(error)
                continue
            reach = ReachOut(email=email[0]
                             ,contact_date=datetime.datetime.now()
                             ,mapper=mapper[0])
            try:           
                reach.sendMessage(opener,cookies)
                reach.save()
                sent.append(reach)
            except ReachOut.SendException,e:
                failed.append(e)
            
    t = loader.get_template('follower/reach_out_sent.html')
    
    c = RequestContext(request,{'sent': sent,
                                'failed' : failed })
    return HttpResponse(t.render(c))


class MapperForm(ModelForm):
    """
    A Form for mapper data.
    """
    class Meta:
        model = Mapper
        exclude = [ 'reach_outs']

class MapperCreateView(CreateView):
    """
    A create view class used for the view that creates new mapper instances
    """
    form_class = MapperForm
    success_url = '/mapper/list'
    initial = {}
    Model = Mapper
    template_name = 'follower/mapper_form.html'


class MapperView(UpdateView):
    queryset = Mapper.objects.all()
    pk_url_kwarg = 'id'
    context_object_name = 'mapper'
    initial = { }
    success_url = '/mapper/list'
    form_class = MapperForm

    def form_valid(self,form):
        form.save()
        return super(UpdateView,self).form_valid(form)

    def form_invalid(self,form):
        return True

    def get_object(self,**kwargs):
        self.object = super(UpdateView,self).get_object(**kwargs)
        return self.object

    def get_context_data(self,**kwargs):
        # Call the base implementation first to get a context
        context = super(UpdateView, self).get_context_data(**kwargs)
        # get a list of all reachouts
        reach_outs = []
        for email in self.object.reach_outs.all():
            reaches = ReachOut.objects.all().filter(email=email,
                                                    mapper=self.object)
            for reach in reaches:
                data = { 'contact_date' : reach.contact_date ,
                         'subject' : reach.email.subject ,
                         'body' : reach.email.text,
                         }
                reach_outs.append(data)
        context['reach_outs'] = reach_outs
        return context



class EmailForm(ModelForm):
    class Meta:
        model = Email


class EmailCreateView(CreateView):
    form_class = EmailForm
    success_url = '/email/list'
    initial = {}
    Model = Email
    template_name = 'follower/email_form.html'


class EmailUpdateView(UpdateView):
    queryset = Email.objects.all()
    pk_url_kwarg = 'id'
    initial = { }
    success_url = "/email/list"

    def get_context_data(self,**kwargs):
        context = super(UpdateView,self).get_context_data(**kwargs)
        context['id'] = self.object.id
        usages=ReachOut.objects.all().filter(email=self.object.id)
        if len(usages) > 0:
            context['sent'] = True
        else:
            context['sent'] = False
        return context

    def form_valid(self,form):
        self.object = self.get_object()
        usages=ReachOut.objects.all().filter(email=self.object.id)
        if len(usages) > 0:
            t = loader.get_template('follower/email_delete_forbidden.html')
            c = Context({'reason': 'This email has already been sent'})
            return HttpResponseForbidden(t.render(c))
        else:
            return super(UpdateView,self).form_valid(form)


class EmailDeleteView(DeleteView):
    success_url = '/email/list'
    Model = Email
    queryset = Email.objects.all()
    pk_url_kwarg = 'id'      

    def delete(self,request,*args,**kwargs):
        """ 
        deletes the email object.
        only delete the email if it hasn't been used
        """
        self.object = self.get_object()
    
        usages = ReachOut.objects.all().filter(email=self.object.id)
        if len(usages) > 0:
             t = loader.get_template('follower/email_delete_forbidden.html')
             c = Context({'reason': 'This email has already been sent'})
             return HttpResponseForbidden(t.render(c))
        else:
            return super(DeleteView,self).delete(request,*args,**kwargs)


class EmailListView(ListView):
    queryset = Email.objects.all()
    context_object_name = 'emails'
    Model = Email

    
def authenticate_osm(username,password):
    """
    establishes a client connection with OSM and authenticates against the API
    This method will return a set of cookies in a CookieJar usable to identify
    the session in future calls
    
    """

    login_payload = {}

    request = urllib2.Request('https://' + settings.OSM_API + '/login')
    cookies = cookielib.CookieJar()
    opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cookies))
    response_tokenfetch = opener.open(request)
    html = response_tokenfetch.read()
    htmlfile = StringIO.StringIO(html)
    
    xml_tree = ElementTree.parse(htmlfile)
    for form in xml_tree.getiterator(formtag):                
        for field in form.getiterator(inptag):
            if 'name' in field.attrib and 'value' in field.attrib:
                login_payload[field.attrib['name']] = field.attrib['value']
    login_payload['username'] = username
    login_payload['password'] = password
    for field in login_payload:
        login_payload[field]=login_payload[field].encode('utf-8')
        #print("%s : %s " % (field, login_payload[field]))

    #cookies.extract_cookies(response_tokenfetch,request)
    print "authenticate_osm:" + str(cookies)
    cookies.add_cookie_header(request)
    response = opener.open(request,urllib.urlencode(login_payload))
    print "authenticate_osm:" + str(response.info())
    print "authenticate_osm:" + str(response.geturl())
    #cookies.extract_cookies(response,request)  
    
    return cookies

