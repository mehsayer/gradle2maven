package web;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import static org.assertj.core.api.Assertions.assertThat;
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class WebAppTest {
    @Autowired TestRestTemplate rest;
    @Test void greets() { assertThat(rest.getForObject("/", String.class)).isEqualTo("Hello webtxt"); }
}
