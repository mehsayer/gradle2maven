package web;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.*;
@SpringBootApplication
@RestController
public class WebApp {
    @GetMapping("/") public String hi() { return new core.Greeter("web").greet(); }
    public static void main(String[] args) { SpringApplication.run(WebApp.class, args); }
}
